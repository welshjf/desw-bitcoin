# Shared Wallet Bitcoin RPC Plugin

Bitcoin RPC plugin for the Deginner Shared Wallet project. Allows sending, receiving, and other generic wallet functionality using Bitcoind over RPC.

# Configuration

This plugin expects a .ini configuration file. Like other desw plugins, this file can be specified by setting the `DESW_CONFIG_FILE` environmental variable, like so.

`export DESW_CONFIG_FILE="path/to/cfg.ini"`

# Testing

This project requires 2 bitcoin testnet nodes. The first should be configured for normal `desw_bitcoin` use, and the second in the `BITCOIN` variable of the `test` section in the config file. Both should have a nominal (>0.5 coin) balance.

```
[bitcoin]
RPCURL: http://bitcoinrpc:pass@127.0.0.1:8332
CONFS: 3

[test]
BITCOIN: http://bitcoinrpc:testpass@remote.server.com:18332
```