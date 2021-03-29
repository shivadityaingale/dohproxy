# DNS Over HTTPS Proxy

A DNS over HTTP proxy server with user specific blocking. 

Original [source](https://github.com/facebookexperimental/doh-proxy).

This project only uses doh-httpproxy and additional server which used to check whether a domain is blocked for specific user or not.

The blocklist can be given in txt file in **user-blocklist** directory.

This POC is done by adding HTTP **authorization** header with Basic  authentication scheme in DOH client.


## Usage

### doh-httpproxy

`doh-httpproxy` is designed to be running behind a reverse proxy. In this setup
a reverse proxy such as [NGINX](https://nginx.org/) would be handling the
HTTPS/HTTP2 requests from the DOH clients and will forward them to
`doh-httpproxy` backends.

While this setup requires more upfront setup, it allows running DOH proxy
unprivileged and on multiple cores.


```shell
$ httpproxy.py \
    --upstream-resolver=::1 \
    --port 8080 \
    --listen-address ::1 \
    --socket /tmp/dnsblockcheck.sock
```

`doh-httpproxy` now also supports TLS, that you can enable passing the 
args `--certfile` and `--keyfile` (just like `doh-proxy`)


### dnsblockcheck

`dnsblockcheck` 

```shell
$ dnsblockcheck.py \
    --socket /tmp/dnsblockcheck.sock \
    --filespath ./user-blocklist
```

The blocklist files can be reloaded by sending **SIGUSR1** signal to dnsblockcheck process.

```
$ pkill -SIGUSR1 -f dnsblockcheck
```


### Requirements

* python >= 3.5
* aiohttp
* aioh2
* dnspython

