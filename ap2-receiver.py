import os
import sys
import time
import struct
import socket
import argparse
import tempfile

import pprint

import http.server
import socketserver

import netifaces as ni
from hexdump import hexdump
from Crypto.Cipher import ChaCha20_Poly1305, AES
from zeroconf import IPVersion, ServiceInfo, Zeroconf
from biplist import readPlistFromString, writePlistToString

from ap2.pairing.hap import Hap, HAPSocket
from ap2.connections.event import Event
from ap2.connections.stream import Stream

# No Auth - coreutils, PairSetupMfi
# MFi Verify fail error after pair-setup[2/5]
FEATURES = 0x88340405f8a00
# No Auth - HK and coreutils
# Stops after pairing (setup [5/5] verify [2/2])with no supported auth error
FEATURES = 0xc340405f8a00
# No Auth = HK, coreutils, PairSetupMFi
# MFi Verify fail error after pair-setup[2/5]
FEATURES = 0x8c340405f8a00
# Mfi Auth - HK and coreutils
# All encrypt after pairing (setup [5/5] verify [2/2])
FEATURES = 0xc340445f8a00
# FairPlay - HK and coreutils
# Stops after pairing (setup [5/5] verify [2/2])with no supported auth error
FEATURES = 0xc340405fca00
# FairPlay - HK and coreutils and transient
# fp-setup after pair-setup[2/5]
FEATURES = 0x1c340405fca00
# MFi - HK and coreutils and transient
# auth-setup after pair-setup[2/5]
FEATURES = 0x1c340445f8a00
# No Auth - No enc - PairSetupMFi
# Works!!
FEATURES = 0x8030040780a00
# No Auth - No enc
# No supported authentication types.
# FEATURES = 0x30040780a00
# FEATURES = 0x8030040780a00 | (1 << 27)

try: #en7 USB interface
    ifen = ni.ifaddresses("en7")
    IFEN = "en7"
except ValueError:
    ifen = ni.ifaddresses("en0")
    IFEN = "en0"

DEVICE_ID = ifen[ni.AF_LINK][0]["addr"]
IPV4 = ifen[ni.AF_INET][0]["addr"]
IPV6 = ifen[ni.AF_INET6][0]["addr"].split("%")[0]

SERVER_VERSION = "366.0"
HTTP_CT_BPLIST = "application/x-apple-binary-plist"
HTTP_CT_OCTET = "application/octet-stream"
HTTP_CT_PARAM = "text/parameters"
HTTP_CT_IMAGE = "image/jpeg"
HTTP_CT_DMAP = "application/x-dmap-tagged"

def setup_global_structs(args):
    global sonos_one_info
    global sonos_one_setup
    global sonos_one_setup_data
    global second_stage_info
    global mdns_props

    sonos_one_info = {
        # 'OSInfo': 'Linux 3.10.53',
        # 'PTPInfo': 'OpenAVNU ArtAndLogic-aPTP-changes a5d7f94-0.0.1',
        'audioLatencies': [   {   'inputLatencyMicros': 0,
                                  'outputLatencyMicros': 400000,
                                  'type': 100},
                              {   'audioType': 'default',
                                  'inputLatencyMicros': 0,
                                  'outputLatencyMicros': 400000,
                                  'type': 100},
                              {   'audioType': 'media',
                                  'inputLatencyMicros': 0,
                                  'outputLatencyMicros': 400000,
                                  'type': 100},
                              {   'audioType': 'media',
                                  'inputLatencyMicros': 0,
                                  'outputLatencyMicros': 400000,
                                  'type': 102}],
        # 'build': '16.0',
        'deviceID': DEVICE_ID,
        'features': FEATURES,
        # 'features': 496155769145856, # Sonos One
        # 'firmwareBuildDate': 'Nov  5 2019',
        # 'firmwareRevision': '53.3-71050',
        # 'hardwareRevision': '1.21.1.8-2',
        'keepAliveLowPower': True,
        'keepAliveSendStatsAsBody': True,
        'manufacturer': 'Sonos',
        'model': 'One',
        'name': 'Camera da letto',
        'nameIsFactoryDefault': False,
        'pi': 'ba5cb8df-7f14-4249-901a-5e748ce57a93', # UUID generated casually..
        'protocolVersion': '1.1',
        'sdk': 'AirPlay;2.0.2',
        'sourceVersion': '366.0',
        'statusFlags': 4,
        # 'statusFlags': 0x404 # Sonos One
        }

    second_stage_info = {
        "initialVolume": -130,
        }

    sonos_one_setup = {
            'eventPort': 0,  # AP2 receiver event server
            'timingPort': 0,
            'timingPeerInfo': {
                'Addresses': [
                    IPV4, IPV6], 
                'ID': IPV4}
            }

    sonos_one_setup_data = {
            'streams': [
                {
                    'type': 96, 
                    'dataPort': 0, # AP2 receiver data server 
                    'controlPort': 0 # AP2 receiver control server
                    }
                ]
            }

    mdns_props = {
            "srcvers": SERVER_VERSION,
            "deviceid": DEVICE_ID,
            "features": "%s,%s" % (hex(FEATURES & 0xffffffff), hex(FEATURES >> 32 & 0xffffffff)),
            "flags": "0x4",
            # "name": "GINO", # random
            # "model": "GIO", # random
            # "manufacturer": "Pino", # random
            # "serialNumber": "01234xX321", # random
            "protovers": "1.1",
            "acl": "0",
            "rsf": "0x0",
            "fv": "p20.78000.12",
            "pi": "5dccfd20-b166-49cc-a593-6abd5f724ddb", # UUID generated casually
            "gid": "5dccfd20-b166-49cc-a593-6abd5f724ddb", # UUID generated casually
            "gcgl": "0",
            # "vn": "65537",
            "pk": "de352b0df39042e201d31564049023af58a106c6d904b74a68aa65012852997f",
            }

class AP2Handler(http.server.BaseHTTPRequestHandler):

    pp = pprint.PrettyPrinter()

    def parse_request(self):
        self.raw_requestline = self.raw_requestline.replace(b"RTSP/1.0", b"HTTP/1.1")

        r = http.server.BaseHTTPRequestHandler.parse_request(self)
        self.protocol_version = "RTSP/1.0"
        self.close_connection = 0
        return r

    def send_response(self, code, message=None):
        if message is None:
            if code in self.responses:
                message = self.responses[code][0]
            else:
                message = b''

        response = "%s %d %s\r\n" % (self.protocol_version, code, message)
        self.wfile.write(response.encode())

    def version_string(self):
        return "AirTunes/%s" % SERVER_VERSION

    def do_GET(self):
        print(self.headers)
        if self.path == "/info":
            print("GET /info")
            self.handle_info()
        else:
            print("GET %s Not implemented!" % self.path)
            self.send_error(404)

    def do_POST(self):
        print(self.headers)
        if self.path == "/command":
            print("POST /command")
            self.handle_command()
        elif self.path == "/feedback":
            print("POST /feedback")
            self.handle_feedback()
        elif self.path == "/audioMode":
            print("POST /audioMode")
            self.handle_audiomode()
        elif self.path == "/auth-setup":
            print("POST /auth-setup")
            self.handle_auth_setup()
        elif self.path == "/fp-setup":
            print("POST /fp-setup")
            self.handle_fp_setup()
        elif self.path == "/fp-setup2":
            print("POST /fp-setup2")
            self.handle_auth_setup()
        elif self.path == "/pair-setup":
            print("POST /pair-setup")
            self.handle_pair_setup()
        elif self.path == "/pair-verify":
            print("POST /pair-verify")
            self.handle_pair_verify()
        else:
            print("POST %s Not implemented!" % self.path)
            self.send_error(404)

    def do_SETUP(self):
        dacp_id = self.headers.get("DACP-ID")
        active_remote = self.headers.get("Active-Remote")
        ua = self.headers.get("User-Agent")
        print("SETUP %s" % self.path)
        print(self.headers)
        if self.headers["Content-Type"] == HTTP_CT_BPLIST:
            content_len = int(self.headers["Content-Length"])
            if content_len > 0:
                body = self.rfile.read(content_len)

                plist = readPlistFromString(body)
                self.pp.pprint(plist)
                if "streams" not in plist:
                    print("Sending EVENT:")
                    event_port, self.event_proc = Event.spawn()
                    sonos_one_setup["eventPort"] = event_port
                    print("[+] eventPort=%d" % event_port)

                    self.pp.pprint(sonos_one_setup)
                    res = writePlistToString(sonos_one_setup)
                    self.send_response(200)
                    self.send_header("Content-Length", len(res))
                    self.send_header("Content-Type", HTTP_CT_BPLIST)
                    self.send_header("Server", self.version_string())
                    self.send_header("CSeq", self.headers["CSeq"])
                    self.end_headers()
                    self.wfile.write(res)
                else:
                    print("Sending CONTROL/DATA:")

                    stream = Stream(plist["streams"][0])
                    self.server.streams.append(stream)
                    sonos_one_setup_data["streams"][0]["controlPort"] = stream.control_port
                    sonos_one_setup_data["streams"][0]["dataPort"] = stream.data_port

                    print("[+] controlPort=%d dataPort=%d" % (stream.control_port, stream.data_port))
                    if stream.type == Stream.BUFFERED:
                        sonos_one_setup_data["streams"][0]["type"] = stream.type
                        sonos_one_setup_data["streams"][0]["audioBufferSize"] = 8388608

                    self.pp.pprint(sonos_one_setup_data)
                    res = writePlistToString(sonos_one_setup_data)

                    self.send_response(200)
                    self.send_header("Content-Length", len(res))
                    self.send_header("Content-Type", HTTP_CT_BPLIST)
                    self.send_header("Server", self.version_string())
                    self.send_header("CSeq", self.headers["CSeq"])
                    self.end_headers()
                    self.wfile.write(res)
                return
        self.send_error(404)

    def do_GET_PARAMETER(self):
        print("GET_PARAMETER %s" % self.path)
        print(self.headers)
        params_res = {}
        content_len = int(self.headers["Content-Length"])
        if content_len > 0:
            body = self.rfile.read(content_len)

            params = body.splitlines()
            for p in params:
                if p == b"volume":
                    print("GET_PARAMETER: %s" % p)
                    params_res[p] = b"-144"
                else:
                    print("Ops GET_PARAMETER: %s" % p)

        res = b"\r\n".join(b"%s: %s" % (k, v) for k, v in params_res.items()) + b"\r\n"
        self.send_response(200)
        self.send_header("Content-Length", len(res))
        self.send_header("Content-Type", HTTP_CT_PARAM)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()
        self.wfile.write(res)

    def do_SET_PARAMETER(self):
        print("SET_PARAMETER %s" % self.path)
        print(self.headers)
        params_res = {}
        content_type = self.headers["Content-Type"]
        content_len = int(self.headers["Content-Length"])
        if content_type == HTTP_CT_PARAM:
            if content_len > 0:
                body = self.rfile.read(content_len)

                params = body.splitlines()
                for p in params:
                    pp = p.split(b":")
                    if pp[0] in [b"volume", b"progress"]:
                        print("SET_PARAMETER: %s => %s" % (pp[0], pp[1]))
                    else:
                        print("Ops SET_PARAMETER: %s" % p)
        elif content_type == HTTP_CT_IMAGE:
            if content_len > 0:
                fname = None
                with tempfile.NamedTemporaryFile(prefix="artwork", dir=".", delete=False) as f:
                    f.write(self.rfile.read(content_len))
                    fname = f.name
                print("Artwork saved to %s" % fname)
        elif content_type == HTTP_CT_DMAP:
            if content_len > 0:
                self.rfile.read(content_len)
                print("Now plaing DAAP info. (need a daap parser here)")
        self.send_response(200)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()

    def do_RECORD(self):
        print("RECORD %s" % self.path)
        print(self.headers)
        if self.headers["Content-Type"] == HTTP_CT_BPLIST:
            content_len = int(self.headers["Content-Length"])
            if content_len > 0:
                body = self.rfile.read(content_len)

                plist = readPlistFromString(body)
                self.pp.pprint(plist)
        self.send_response(200)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()

    def do_SETRATEANCHORTIME(self):
        print("SETRATEANCHORTIME %s" % self.path)
        print(self.headers)
        if self.headers["Content-Type"] == HTTP_CT_BPLIST:
            content_len = int(self.headers["Content-Length"])
            if content_len > 0:
                body = self.rfile.read(content_len)

                plist = readPlistFromString(body)
                self.pp.pprint(plist)
        self.send_response(200)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()

    def do_TEARDOWN(self):
        print("TEARDOWN %s" % self.path)
        print(self.headers)
        if self.headers["Content-Type"] == HTTP_CT_BPLIST:
            content_len = int(self.headers["Content-Length"])
            if content_len > 0:
                body = self.rfile.read(content_len)

                plist = readPlistFromString(body)
                if "streams" in plist:
                    stream_id = plist["streams"][0]["streamID"]
                    stream = self.server.streams[stream_id]
                    stream.teardown()
                    del self.server.streams[stream_id]
                self.pp.pprint(plist)
        self.send_response(200)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()

    def do_SETPEERS(self):
        print("SETPEERS %s" % self.path)
        print(self.headers)
        content_len = int(self.headers["Content-Length"])
        if content_len > 0:
            body = self.rfile.read(content_len)

            plist = readPlistFromString(body)
            self.pp.pprint(plist)
        self.send_response(200)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()

    def do_FLUSH(self):
        print("FLUSH %s" % self.path)
        print(self.headers)
        self.send_response(200)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()

    def handle_command(self):
        if self.headers["Content-Type"] == HTTP_CT_BPLIST:
            content_len = int(self.headers["Content-Length"])
            if content_len > 0:
                body = self.rfile.read(content_len)

                plist = readPlistFromString(body)
                newin = []
                if "mrSupportedCommandsFromSender" in plist["params"]:
                    for p in plist["params"]["mrSupportedCommandsFromSender"]:
                        iplist = readPlistFromString(p)
                        newin.append(iplist)
                    plist["params"]["mrSupportedCommandsFromSender"] = newin
                if "params" in plist["params"] and "kMRMediaRemoteNowPlayingInfoArtworkData" in plist["params"]["params"]:
                    plist["params"]["params"]["kMRMediaRemoteNowPlayingInfoArtworkData"] = "<redacted ..too long>"
                self.pp.pprint(plist)
        self.send_response(200)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()

    def handle_feedback(self):
        if self.headers["Content-Type"] == HTTP_CT_BPLIST:
            content_len = int(self.headers["Content-Length"])
            if content_len > 0:
                body = self.rfile.read(content_len)

                plist = readPlistFromString(body)
                self.pp.pprint(plist)
        self.send_response(200)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()

    def handle_audiomode(self):
        if self.headers["Content-Type"] == HTTP_CT_BPLIST:
            content_len = int(self.headers["Content-Length"])
            if content_len > 0:
                body = self.rfile.read(content_len)

                plist = readPlistFromString(body)
                self.pp.pprint(plist)
        self.send_response(200)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()

    def handle_auth_setup(self):
        content_len = int(self.headers["Content-Length"])
        if content_len > 0:
            body = self.rfile.read(content_len)
            hexdump(body)

        self.send_response(200)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()

    def handle_fp_setup(self):
        content_len = int(self.headers["Content-Length"])
        if content_len > 0:
            body = self.rfile.read(content_len)
            hexdump(body)

        self.send_response(200)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()

    def handle_pair_setup(self):
        content_len = int(self.headers["Content-Length"])

        body = self.rfile.read(content_len)
        hexdump(body)

        if not self.server.hap:
            self.server.hap = Hap()
        res = self.server.hap.pair_setup(body)

        self.send_response(200)
        self.send_header("Content-Length", len(res))
        self.send_header("Content-Type", HTTP_CT_BPLIST)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()
        self.wfile.write(res)

        if self.server.hap.encrypted:
            hexdump(self.server.hap.accessory_shared_key)
            self.upgrade_to_encrypted(self.server.hap.accessory_shared_key)

    def handle_pair_verify(self):
        content_len = int(self.headers["Content-Length"])

        body = self.rfile.read(content_len)

        if not self.server.hap:
            self.server.hap = Hap()
        res = self.server.hap.pair_verify(body)

        self.send_response(200)
        self.send_header("Content-Length", len(res))
        self.send_header("Content-Type", HTTP_CT_OCTET)
        self.send_header("Server", self.version_string())
        self.send_header("CSeq", self.headers["CSeq"])
        self.end_headers()
        self.wfile.write(res)

        if self.server.hap.encrypted:
            hexdump(self.server.hap.accessory_shared_key)
            self.upgrade_to_encrypted(self.server.hap.accessory_shared_key)

    def handle_info(self):
        if "Content-Type" in self.headers:
            if self.headers["Content-Type"] == HTTP_CT_BPLIST:
                content_len = int(self.headers["Content-Length"])
                if content_len > 0:
                    body = self.rfile.read(content_len)

                    plist = readPlistFromString(body)
                    self.pp.pprint(plist)
                    if "qualifier" in plist and "txtAirPlay" in plist["qualifier"]:
                        print("Sending:")
                        self.pp.pprint(sonos_one_info)
                        res = writePlistToString(sonos_one_info)

                        self.send_response(200)
                        self.send_header("Content-Length", len(res))
                        self.send_header("Content-Type", HTTP_CT_BPLIST)
                        self.send_header("Server", self.version_string())
                        self.send_header("CSeq", self.headers["CSeq"])
                        self.end_headers()
                        self.wfile.write(res)
                    else:
                        print("No txtAirPlay")
                        self.send_error(404)
                        return
                else:
                    print("No content")
                    self.send_error(404)
                    return
            else:
                print("Content-Type: %s | Not implemented" % self.headers["Content-Type"])
                self.send_error(404)
        else:
            res = writePlistToString(second_stage_info)
            self.send_response(200)
            self.send_header("Content-Length", len(res))
            self.send_header("Content-Type", HTTP_CT_BPLIST)
            self.send_header("Server", self.version_string())
            self.send_header("CSeq", self.headers["CSeq"])
            self.end_headers()
            self.wfile.write(res)

    def upgrade_to_encrypted(self, shared_key):
        self.request = self.server.upgrade_to_encrypted(
                                    self.client_address,
                                    shared_key)
        self.connection = self.request
        self.rfile = self.connection.makefile('rb', self.rbufsize)
        self.wfile = self.connection.makefile('wb')
        self.is_encrypted = True
        print("----- ENCRYPTED CHANNEL -----")

def register_mdns(receiver_name):
    addresses = []
    for ifen in ni.interfaces():
        ifenaddr = ni.ifaddresses(ifen)
        if ni.AF_INET in ifenaddr:
            addresses.append(socket.inet_pton(ni.AF_INET,
                ifenaddr[ni.AF_INET][0]["addr"]))
        if ni.AF_INET6 in ifenaddr:
            addresses.append(socket.inet_pton(ni.AF_INET6,
                ifenaddr[ni.AF_INET6][0]["addr"].split("%")[0]))

    info = ServiceInfo(
            "_airplay._tcp.local.",
            "%s._airplay._tcp.local." % receiver_name,
            # addresses=[socket.inet_aton("127.0.0.1")],
            addresses=addresses,
            port=7000,
            properties=mdns_props,
            server="%s.local." % receiver_name,
            )

    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    zeroconf.register_service(info)
    print("mDNS service registered")
    return (zeroconf, info)

def unregister_mdns(zeroconf, info):
    print("Unregistering...")
    zeroconf.unregister_service(info)
    zeroconf.close()


def get_free_port():
    free_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free_socket.bind(('0.0.0.0', 0))
    free_socket.listen(5)
    port = free_socket.getsockname()[1]
    free_socket.close()
    return port


class AP2Server(socketserver.TCPServer):

    def __init__(self, addr_port, handler):
        super().__init__(addr_port, handler)
        self.connections = {}
        self.hap = None
        self.enc_layer = False
        self.streams = []

    #Override
    def get_request(self):
        client_socket, client_addr = super().get_request()
        print("Got connection with %s:%d" % client_addr)
        self.connections[client_addr] = client_socket
        return (client_socket, client_addr)

    def upgrade_to_encrypted(self, client_address, shared_key):
        client_socket = self.connections[client_address]
        hap_socket = HAPSocket(client_socket, shared_key)
        self.connections[client_address] = hap_socket
        return hap_socket

if __name__ == "__main__":

    parser = argparse.ArgumentParser(prog='AirPlay 2 receiver')
    parser.add_argument("-m", "--mdns", required=True, help="mDNS name to announce")
    args = parser.parse_args()

    setup_global_structs(args)

    print("Interface: %s" % IFEN)
    print("IPv4: %s" % IPV4)
    print("IPv6: %s" % IPV6)
    print()

    mdns = register_mdns(args.mdns)
    print("Starting RSTP server, press Ctrl-C to exit...")
    try:
        PORT = 7000

        with AP2Server(("0.0.0.0", PORT), AP2Handler) as httpd:
            print("serving at port", PORT)
            httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down mDNS...")
        unregister_mdns(*mdns)
