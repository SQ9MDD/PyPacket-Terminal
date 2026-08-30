# PyPacket

PyPacket is a modern packet-radio terminal and AX.25 backend for KISS TNCs.

It is designed for direct operation with real radios, TNCs and Direwolf, while keeping the user interface simple and familiar.

## Features

- AX.25 connected mode over KISS TCP
- Multiple simultaneous KISS ports
- Direct and VIA connections
- Multi-port DIGI operation
- UI frame fan-out across DIGI ports
- Dynamic route learning from MHeard
- Direct routes preferred over VIA routes
- Persistent MHeard list
- Periodic and manual UI beacons
- Native JSON API
- AGWPE compatibility interface
- Headless station service
- Windows, macOS and Linux support

## Files

```text
pypacket_terminal.py   Graphical terminal
pypacket_backend.py    AX.25 / KISS backend
config.json            Runtime configuration
LICENSE                MIT License
```

The terminal starts the backend automatically when needed.

## Requirements

- Python 3.11 or newer
- Tkinter
- KISS TCP compatible TNC, modem or Direwolf

## Running

Keep `pypacket_terminal.py` and `pypacket_backend.py` in the same directory.

Start the terminal:

```bash
python3 pypacket_terminal.py
```

On Windows:

```text
python pypacket_terminal.py
```

Configure KISS ports from:

```text
File -> Configure Ports...
```

## Network interfaces

By default PyPacket provides:

```text
AGWPE TCP : 8000
JSON API  : 8010
```

KISS TCP endpoints are configured by the user.

## License

PyPacket is released under the MIT License.

Copyright (c) 2026 SQ9MDD
