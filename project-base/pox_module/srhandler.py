from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.revent import *
from pox.lib.util import dpidToStr
from pox.lib.util import str_to_bool
from pox.lib.recoco import Timer
from pox.lib.packet import ethernet,arp,ipv4,icmp,udp,tcp
import time

import threading
import asyncore
import collections
import logging
import socket

# Required for VNS
import sys
import os
from twisted.python import threadable
from threading import Thread

from twisted.internet import reactor
from VNSProtocol import VNS_DEFAULT_PORT, create_vns_server
from VNSProtocol import VNSOpen, VNSClose, VNSPacket, VNSOpenTemplate, VNSBanner
from VNSProtocol import VNSRtable, VNSAuthRequest, VNSAuthReply, VNSAuthStatus, VNSInterface, VNSHardwareInfo

log = core.getLogger()

abort = False

def pack_mac(macaddr):
  octets = macaddr.split(':')
  ret = ''
  for byte in octets:
    ret += chr(int(byte, 16))
  return ret

def pack_ip(ipaddr):
  octets = ipaddr.split('.')
  ret = ''
  for byte in octets:
    ret += chr(int(byte))
  return ret

class SRServerListener(EventMixin):
  ''' TCP Server to handle connection to SR '''
  def __init__ (self, address=('127.0.0.1', 8888)):
    port = address[1]
    self.listenTo(core.cs123_ofhandler)
    self.srclients = {}
    self.srclients_reverse = {}
    self.listen_port = port
    self.intfname_to_port = {}
    self.port_to_intfname = {}
    self.interfaces = {}
    self.edges = set(['sw2', 'sw3', 'sw4'])
    self.counters = {}
    self.trace = {}
    self.timer = {}
    self.server = create_vns_server(port,
                                    self._handle_recv_msg,
                                    self._handle_new_client,
                                    self._handle_client_disconnected)
    log.info('created server')
    return

  def on_pkt(self, pkt, vhost, port, ingress=True):
    # TIMER_NAME = 'gc_trace'
    # GC_ITVL_SEC = 2
    # TRACE_EXP_SEC = 4
    global abort
    if abort:
      return False
    hdr_eth = ethernet(raw=pkt)
    pkt_str = str(hdr_eth) + str(hdr_eth.next)
    if isinstance(hdr_eth.next, ipv4):
      pkt_str += str(hdr_eth.next.next)
    if vhost not in self.counters:
      self.counters[vhost] = 0
    self.counters[vhost] += 1
    if not isinstance(hdr_eth.next, arp) and not ingress:
      # sig = hash(pkt)
      sig = hdr_eth.next.id 
      if sig not in self.trace:
        self.trace[sig] = {
          'init'  : time.time(),
          'nhops' : 0,
          'hops'  : set(),
          'path'  : []
        }
      self.trace[sig]['nhops'] += 1
      self.trace[sig]['path'].append(vhost)
      if vhost in self.trace[sig]['hops']:
        log.debug("Loop detected? {}, packet: {}".format(self.trace[sig]['path'], pkt_str))
      self.trace[sig]['hops'].add(vhost)
    if ingress:
      log.debug("{} packet-in <<{}>> from port {}".format(vhost, pkt_str, port))
    else:
      log.debug("{} packet-out <<{}>> to port {}".format(vhost, pkt_str, port))
    # elif vhost in self.edges and port == 1:
    # log.debug("counters {}".format(self.counters))
    # log.debug(self.trace)
    # if TIMER_NAME not in self.timer:
    #   self.timer[TIMER_NAME] = time.time()
    # now = time.time()
    # if now - self.timer[TIMER_NAME] > GC_ITVL_SEC:
    #   purgable = []
    #   for sig in self.trace:
    #     if now - self.trace[sig]['init'] > TRACE_EXP_SEC:
    #       purgable.append(sig)
    #   for sig in purgable:
    #     self.trace.pop(sig)
    return True

  def broadcast(self, message):
    log.debug('Broadcasting message: %s', message)
    for client in self.srclients.values():
      client.send(message)

  def send_to_vhost(self, message, vhost):
    # log.debug('Unicast message to %s: %s', vhost, message)
    vhost_conn = self.srclients[vhost]
    vhost_conn.send(message)

  def _handle_SRPacketIn(self, event):
    # log.debug("SRPacketIn event, port=%d, pkt=%s, vhost=%s" % (event.port, ethernet(raw=event.pkt), event.vhost))
    try:
        # log.info('Lookup for %s %s' % (event.vhost, event.port,))
        intfname = self.port_to_intfname[event.vhost][event.port]
    except KeyError:
        log.debug("Couldn't find interface for portnumber %s" % event.port)
        return
    #print "srpacketin, packet=%s" % ethernet(event.pkt)
    if self.on_pkt(event.pkt, event.vhost, event.port):
      self.send_to_vhost(VNSPacket(intfname, event.pkt), event.vhost)

  def _handle_RouterInfo(self, event):
    log.debug("SRServerListener catch RouterInfo even for vhost=%s, info=%s, rtable=%s", event.vhost, event.info, event.rtable)
    sw_info = event.info
    interfaces = []
    for intf in sw_info.keys():
      ip, mac, rate, port = sw_info[intf]
      ip = pack_ip(ip)
      mac = pack_mac(mac)
      mask = pack_ip('255.255.255.255')
      interfaces.append(VNSInterface(intf, mac, ip, mask))
      # Mapping between of-port and intf-name
      if( event.vhost not in self.intfname_to_port.keys()):
        self.intfname_to_port[event.vhost] = {}
        self.port_to_intfname[event.vhost] = {}
      self.intfname_to_port[event.vhost][intf] = port
      log.info('%s %s %s' % (event.vhost,intf,port,))
      self.port_to_intfname[event.vhost][port] = intf
    # store the list of interfaces...
    self.interfaces[event.vhost] = interfaces
    
  def _handle_recv_msg(self, conn, vns_msg):
    # demux sr-client messages and take approriate actions
    if vns_msg is None:
      log.debug("invalid message")
      self._handle_close_msg(conn)
      return
    # log.debug('recv VNS msg: %s' % vns_msg)
    if vns_msg.get_type() == VNSAuthReply.get_type():
      self._handle_auth_reply(conn)
      return
    elif vns_msg.get_type() == VNSOpen.get_type():
      self._handle_open_msg(conn, vns_msg)
    elif vns_msg.get_type() == VNSClose.get_type():
      self._handle_close_msg(conn)
    elif vns_msg.get_type() == VNSPacket.get_type():
      self._handle_packet_msg(conn, vns_msg)
    elif vns_msg.get_type() == VNSOpenTemplate.get_type():
      # TODO: see if this is needed...
      self._handle_open_template_msg(conn, vns_msg)
    else:
      log.debug('unexpected VNS message received: %s' % vns_msg)

  def _handle_auth_reply(self, conn):
    # always authenticate
    msg = "authenticated %s as %s" % (conn, 'user')
    conn.send(VNSAuthStatus(True, msg))

  def _handle_new_client(self, conn):
    log.debug('Accepted client at %s' % conn.transport.getPeer().host)
    # send auth message to drive the sr-client state machine
    salt = os.urandom(20)
    conn.send(VNSAuthRequest(salt))
    return

  def _handle_client_disconnected(self, conn):
    log.info("disconnected")
    conn.transport.loseConnection()
    return

  def _handle_open_msg(self, conn, vns_msg):
    # client wants to connect to some topology.
    log.debug("open-msg: %s, vhost:%s" % (vns_msg.topo_id, vns_msg.vhost))
    self.srclients['%s' % vns_msg.vhost] = conn
    self.srclients_reverse[conn] = '%s' % vns_msg.vhost
    
    print self.interfaces['%s' % vns_msg.vhost]
    try:
      conn.send(VNSHardwareInfo(self.interfaces['%s' % vns_msg.vhost]))
    except:
      log.debug('interfaces not populated yet')  
    return

  def _handle_close_msg(self, conn):
    conn.send("Goodbyte!") # spelling mistake intended...
    conn.transport.loseConnection()
    return

  def _handle_packet_msg(self, conn, vns_msg):
    # log.debug('VNS Packet msg: %s' % vns_msg)
    out_intf = vns_msg.intf_name
    pkt = vns_msg.ethernet_frame
    vhost = self.srclients_reverse[conn]
    try:
      out_port = self.intfname_to_port[vhost][out_intf]
    except KeyError:
      log.debug('packet-out through wrong port number %s' % out_port)
      return
    # log.debug("packet-out %s: %r" % (out_intf, pkt))
    # log.debug('SRServerHandler raise packet out event')
    if self.on_pkt(pkt, vhost, out_port, ingress=False):
      core.cs123_srhandler.raiseEvent(SRPacketOut(pkt, out_port, vhost))
  
  def _handle_open_template_msg(self, conn, vns_msg): 
    self._handle_open_msg(conn, vns_msg)

class SRPacketOut(Event):
  '''Event to raise upon receiving a packet back from SR'''

  def __init__(self, packet, port, vhost):
    Event.__init__(self)
    self.pkt = packet
    self.port = port
    self.vhost = vhost

class cs123_srhandler(EventMixin):
  _eventMixin_events = set([SRPacketOut])

  def __init__(self):
    EventMixin.__init__(self)
    self.listenTo(core)
    #self.listenTo(core.cs123_ofhandler)
    self.server = SRServerListener()
    log.debug("SRServerListener listening on %s" % self.server.listen_port)
    # self.server_thread = threading.Thread(target=asyncore.loop)
    # use twisted as VNS also used Twisted.
    # its messages are already nicely defined in VNSProtocol.py
    self.server_thread = threading.Thread(target=lambda: reactor.run(installSignalHandlers=False))
    self.server_thread.daemon = True
    self.server_thread.start()

  def _handle_GoingDownEvent (self, event):
    log.debug("Shutting down SRServer")
    del self.server

def launch (transparent=False):
  """
  Starts the SR handler application.
  """
  core.registerNew(cs123_srhandler)