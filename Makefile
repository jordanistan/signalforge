PYTHON ?= .venv/bin/python
# `make run` settings; override on the command line, e.g. make run LOCATION=30.3,-97.7 HTTP_PORT=8766
LOCATION ?= 30.267,-97.743
SERIAL ?= /dev/ttyUSB0
HTTP_PORT ?= 8765
ALLOW ?= !02ecdcdc

.PHONY: setup test test-unit check verify demo doctor run run-bridge start stop status
setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) -m pytest -q

test-unit:
	$(PYTHON) -m pytest -q --ignore=tests/test_web_e2e.py

check:
	$(PYTHON) -m compileall -q tools tests
	@for script in tools/*.sh signalforge.sh start.sh stop.sh; do bash -n "$$script" || exit; done
	@for script in web/*.js; do node --check "$$script" || exit; done

verify: check test

demo:
	$(PYTHON) tools/meshweb.py --demo

doctor:
	$(PYTHON) tools/doctor.py

# Full console: mesh radio, every installed SDR mode, weather and satellites. Owns the serial port and the dongle.
run:
	$(PYTHON) -u tools/meshweb.py -p $(SERIAL) --http-port $(HTTP_PORT) --home $(LOCATION)

# The same plus the Claude bridge (MSG/FOLLOW lines on stdout, replies from run/outbox/).
run-bridge:
	$(PYTHON) -u tools/meshweb.py -p $(SERIAL) --http-port $(HTTP_PORT) --home $(LOCATION) --bridge --allow '$(ALLOW)'

# The full console in the background (signalforge.sh; log in run/signalforge.log). Same variables as `make run`.
start:
	SERIAL=$(SERIAL) HTTP_PORT=$(HTTP_PORT) LOCATION=$(LOCATION) ALLOW='$(ALLOW)' ./signalforge.sh start

stop:
	./signalforge.sh stop

status:
	SERIAL=$(SERIAL) ./signalforge.sh status
