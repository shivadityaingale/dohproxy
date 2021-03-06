#!/usr/bin/env python3
#
# Copyright (c) 2018-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
#
import argparse
import asyncio
import base64
import binascii
import ipaddress
import logging
import ssl
import struct
import sys
import urllib.parse

import dns.edns
import dns.exception
import dns.message
import dns.rcode

try:
    import netifaces
except ImportError as e:
    # Optional module
    netifaces = e
from typing import Dict, List, Optional, Tuple

#from dohproxy import __version__, constants, server_protocol
import constants, server_protocol
__version__ = "0.0.9"


def get_client_ip(transport: asyncio.BaseTransport) -> Tuple[str, None]:
    """ Helper function to return the IP of the client connecting to us.
    Returns None on error.
    """
    peername = transport.get_extra_info("peername")
    if peername:
        return peername[0]
    return None


def msg2question(msg: dns.message.Message) -> str:
    """ Helper function to return a string of name class and type
    """
    question = "<empty>"
    if len(msg.question):
        q = msg.question[0]
        name = q.name.to_text()
        qclass = dns.rdataclass.to_text(q.rdclass)
        qtype = dns.rdatatype.to_text(q.rdtype)
        question = " ".join([name, qtype, qclass])
    return question


def msg2flags(msg: dns.message.Message) -> str:
    """ Helper function to return flags in a message
    """
    return "/".join(dns.flags.to_text(msg.flags).split(" "))


def sum_items(section: List[dns.rrset.RRset]) -> int:
    """ Helper function to return items in a section of dns answer
    """
    return sum(len(x) for x in section)


def dnsquery2log(msg: dns.message.Message) -> str:
    """ Helper function to return a readable excerpt from a dns query object.
    """
    question = msg2question(msg)
    flags = msg2flags(msg)

    return "{} {} {}".format(question, msg.id, flags,)


def dnsans2log(msg: dns.message.Message) -> str:
    """ Helper function to return a readable excerpt from a dns answer object.
    """
    question = msg2question(msg)
    flags = msg2flags(msg)

    return "{} {} {} {}/{}/{} {}/{}/{} {}".format(
        question,
        msg.id,
        flags,
        sum_items(msg.answer),
        sum_items(msg.authority),
        sum_items(msg.additional),
        msg.edns,
        msg.ednsflags,
        msg.payload,
        dns.rcode.to_text(msg.rcode()),
    )


def extract_path_params(url: str) -> Tuple[str, Dict[str, List[str]]]:
    """ Given a URI, extract the path and the parameters
    """
    p = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(p.query, keep_blank_values=True)
    return p.path, params


def create_ssl_context(
    options: argparse.Namespace, http2: bool = False
) -> ssl.SSLContext:
    """ Create SSL Context for the proxies
    :param options: where to find the certile and the keyfile
    :param http2: enable http2 into the context
    :return: An instance of ssl.SSLContext to be used by the proxies
    """

    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(options.certfile, keyfile=options.keyfile)
    if http2:
        ctx.set_alpn_protocols(["h2"])
    ctx.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_COMPRESSION
    ctx.set_ciphers(constants.DOH_CIPHERS)

    return ctx


def create_custom_ssl_context(
    *, insecure: bool, cafile: Optional[str] = None
) -> ssl.SSLContext:
    """ Create a custom SSL context
    :param insecure: Disable certificate verification if True
    :param cafile: Pass custom CA file for cert verification
    :return: An instance of ssl.SSLContext with our configuration
    """

    if insecure:
        sslctx = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
        sslctx.options |= ssl.OP_NO_SSLv2
        sslctx.options |= ssl.OP_NO_SSLv3
        sslctx.options |= ssl.OP_NO_COMPRESSION
        sslctx.set_default_verify_paths()
    else:
        sslctx = ssl.create_default_context()
        if cafile:
            sslctx.load_verify_locations(cafile=cafile, capath=None)

    sslctx.set_alpn_protocols(constants.DOH_H2_NPN_PROTOCOLS)

    return sslctx


def extract_ct_body(params: Dict[str, List[str]]) -> Tuple[str, bytes]:
    """ Extract the content type and body from a list of get parameters.
    :param params: A dictionary of key/value of parameters as provided by
        urllib.parse.parse_qs
    :return: a tuple that contains a string and bytes, respectively ct and
        body.
    :raises: a DOHParamsException with an explanatory message.
    """
    ct = constants.DOH_MEDIA_TYPE
    if constants.DOH_DNS_PARAM in params and len(params[constants.DOH_DNS_PARAM]):
        try:
            body = doh_b64_decode(params[constants.DOH_DNS_PARAM][0])
        except binascii.Error:
            raise server_protocol.DOHParamsException(b"Invalid Body Parameter")
        if not body:
            raise server_protocol.DOHParamsException(b"Missing Body")
    else:
        raise server_protocol.DOHParamsException(b"Missing Body Parameter")

    return ct, body


def dns_query_from_body(body: bytes, debug: bool = False) -> dns.message.Message:
    """ Given a bytes-object, attempt to unpack a DNS Message.
    :param body: the bytes-object wired representation of a DNS message.
    :param debug: a boolean. When True, The error message sent to client will
    be more meaningful.
    :return: a dns.message.Message on success, raises DOHDNSException
    otherwise.
    """
    exc = b"Malformed DNS query"
    try:
        return dns.message.from_wire(body)
    except Exception as e:
        if debug:
            exc = str(e).encode("utf-8")
    raise server_protocol.DOHDNSException(exc)


def doh_b64_encode(s: bytes) -> str:
    """Base 64 urlsafe encode and remove padding.
    :param s: input bytes-like object to be encoded.
    :return: urlsafe base 64 encoded string.
    """
    return base64.urlsafe_b64encode(s).decode("utf-8").rstrip("=")


def doh_b64_decode(s: str) -> bytes:
    """Base 64 urlsafe decode, add padding as needed.
    :param s: input base64 encoded string with potentially missing padding.
    :return: decodes bytes
    """
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def build_query_params(dns_query):
    """Given a wire-format DNS query, build the query parameters.
    """
    return {
        constants.DOH_DNS_PARAM: doh_b64_encode(dns_query),
    }


def make_url(domain, uri):
    """Utility function to return a URL ready to use from a browser or cURL....
    """
    p = urllib.parse.ParseResult(
        scheme="https", netloc=domain, path=uri, params="", query="", fragment="",
    )
    return urllib.parse.urlunparse(p)


def client_parser_base():
    """Build a ArgumentParser object with all the default arguments that are
    useful to both client and stub.
    :return: a ArgumentParser object with the common client side arguments set.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--domain",
        default="localhost",
        help="Domain to make DOH request against. Default: [%(default)s]",
    )
    parser.add_argument(
        "--uri", default=constants.DOH_URI, help="DNS API URI. Default [%(default)s]",
    )
    parser.add_argument(
        "--remote-address",
        help="Remote address where the DOH proxy is running. If None, "
        "--domain will be resolved to lookup and IP. Default: [%(default)s]",
    )
    parser.add_argument(
        "--port", default=443, help="Port to connect to. Default: [%(default)s]"
    )
    parser.add_argument(
        "--post", action="store_true", help="Use HTTP POST instead of GET."
    )
    parser.add_argument(
        "--debug", action="store_true", help="Prints some debugging output",
    )
    parser.add_argument(
        "--level", default="DEBUG", help="log level [%(default)s]",
    )
    parser.add_argument(
        "--cafile", default=None, help="Specify custom CA file for cert verification"
    )
    parser.add_argument(
        "--insecure", action="store_true", help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--version", action="version", version="%(prog)s {}".format(__version__)
    )
    return parser


def proxy_parser_base(*, port: int, secure: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--listen-address",
        default=["::1"],
        nargs="+",
        help="A list of addresses the proxy should listen on. "
        '"all" for all detected interfaces and addresses (netifaces '
        "required). Default: [%(default)s]",
    )
    parser.add_argument(
        "--port",
        "--listen-port",
        default=port,
        type=int,
        help="Port to listen on. Default: [%(default)s]",
    )
    parser.add_argument("--certfile", help="SSL cert file.", required=secure)
    parser.add_argument("--keyfile", help="SSL key file.", required=secure)
    parser.add_argument(
        "--upstream-resolver",
        default="::1",
        help="Upstream recursive resolver to send the query to. "
        "Default: [%(default)s]",
    )
    parser.add_argument(
        "--upstream-port",
        default=53,
        help="Upstream recursive resolver port to send the query to. "
        "Default: [%(default)s]",
    )
    parser.add_argument(
        "--uri", default=constants.DOH_URI, help="DNS API URI. Default [%(default)s]",
    )
    parser.add_argument(
        "--level", default="DEBUG", help="log level [%(default)s]",
    )
    parser.add_argument("--debug", action="store_true", help="Debugging messages...")
    parser.add_argument(
        "--version", action="version", version="%(prog)s {}".format(__version__),
    )
    parser.add_argument(
        "--ecs", action="store_true", help="Enable EDNS Client Subnet (ECS)"
    )
    return parser


def configure_logger(name="", level="DEBUG"):
    """
    :param name: (optional) name of the logger, default: ''.
    :param level: (optional) level of logging, default: DEBUG.
    :return: a logger instance.
    """
    #log_format = "%(name)s - %(funcName)s - %(threadName)s - %(levelname)s: %(message)s"
    log_format = "%(name)s/%(funcName)s %(levelname)s: %(message)s"
    if sys.stdout.isatty():
        # If this is a TTY (e.g. not running in a service manager),
        # prepend the time to log messages
        log_format = "%(asctime)s: " + log_format
    logging.basicConfig(format=log_format)
    logger = logging.getLogger(name)
    level_name = level.upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise Exception("Invalid log level name : %s" % level_name)
    logger.setLevel(level)
    return logger


def get_system_addresses():
    """Get all IPv4/IPv6 addresses listening on the system.
    :return: List of addresses.
    """
    if isinstance(netifaces, ImportError):
        raise netifaces

    addresses = set()
    for iface in netifaces.interfaces():
        iface_addresses = netifaces.ifaddresses(iface)
        for family in (netifaces.AF_INET, netifaces.AF_INET6):
            if family not in iface_addresses:
                continue
            addresses.update(
                [f["addr"] for f in iface_addresses[family] if "addr" in f]
            )
    return list(addresses)


def handle_dns_tcp_data(data, cb):
    """Handle TCP data_received DNS data.
    When enough data is received to assemble a DNS message, a
    callback is called and the remaining data (if any) is returned.
    :param data: Incoming bytes data.
    :param cb: Callback to call when a full TCP DNS message is received.
    :return: Any remaining bytes not fed to the callback.
    """
    if len(data) < 2:
        return data
    msglen = struct.unpack("!H", data[0:2])[0]
    while msglen + 2 <= len(data):
        dnsq = dns.message.from_wire(data[2 : msglen + 2])
        cb(dnsq)
        data = data[msglen + 2 :]
        if len(data) < 2:
            return data
        msglen = struct.unpack("!H", data[0:2])[0]
    return data


def set_dns_ecs(dnsq, ip):
    """Sets RFC 7871 EDNS Client Subnet (ECS) option in a DNS packet.
    An existing ECS option will not be overwritten if present.
    :param dnsq: DNS packet.
    :param ip: IP address. String or ipaddress object.
    :return: Whether ECS was set (bool)
    """
    options = []
    for option in dnsq.options:
        if isinstance(option, dns.edns.ECSOption):
            return False
        options.append(option)

    if not isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        ip = ipaddress.ip_address(ip)
    ip_supernet_bits = 56 if ip.version == 6 else 24
    ip_supernet = ipaddress.ip_network(ip).supernet(new_prefix=ip_supernet_bits,)

    options.append(
        dns.edns.ECSOption(
            address=ip_supernet.network_address.compressed, srclen=ip_supernet_bits,
        )
    )
    dnsq.use_edns(edns=0, ednsflags=dnsq.ednsflags, options=options)
    return True
