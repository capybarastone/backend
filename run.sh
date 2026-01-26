#!/usr/bin/env bash

# TODO: can this run on Windows?
# (does it need to?)

[[ ! -d venv ]] && python3 -m venv venv
./venv/bin/pip install -r requirements.txt

./venv/bin/python server.py