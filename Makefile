# skreenshot: the front door for installing, running and testing.
# `make` or `make help` lists the targets.

SHELL := /bin/sh
PYTHON ?= python3
PREFIX ?= $(HOME)/.local
BINDIR := $(PREFIX)/bin
ICONDIR := $(PREFIX)/share/icons/hicolor
ROOT := $(abspath .)
VENV := .venv
VENV_PY := $(VENV)/bin/python
DEPS_RUN := $(VENV)/.deps-run
DEPS_DEV := $(VENV)/.deps-dev

.DEFAULT_GOAL := help

.PHONY: help deps deps-dev install uninstall install-hotkey uninstall-hotkey run test e2e e2e-wayland lint icons clean

help: ## list all targets with what they do
	@echo "skreenshot make targets:"
	@awk -F':.*## ' '/^[a-z0-9-]+:.*## / { printf "  %-18s %s\n", $$1, $$2 }' Makefile

install: $(DEPS_RUN) ## symlink skreenshot into ~/.local/bin, install icons and .desktop
	mkdir -p $(BINDIR)
	ln -sfn $(ROOT)/skreenshot $(BINDIR)/skreenshot
	for size in 48 128 256; do \
		mkdir -p $(ICONDIR)/$${size}x$${size}/apps; \
		cp icons/skreenshot-$${size}.png $(ICONDIR)/$${size}x$${size}/apps/skreenshot.png; \
	done
	mkdir -p $(PREFIX)/share/applications
	cp skreenshot.desktop $(PREFIX)/share/applications/skreenshot.desktop
	@echo "installed: $(BINDIR)/skreenshot (make sure $(BINDIR) is on PATH)"

uninstall: ## remove the ~/.local/bin symlink, icons and .desktop
	rm -f $(BINDIR)/skreenshot
	for size in 48 128 256; do \
		rm -f $(ICONDIR)/$${size}x$${size}/apps/skreenshot.png; \
	done
	rm -f $(PREFIX)/share/applications/skreenshot.desktop
	@echo "uninstalled (hotkey binding, if any, is removed by uninstall-hotkey)"

install-hotkey: ## bind Shift+Super+S to skreenshot (XFCE or KDE)
	$(ROOT)/skreenshot --install-hotkey

uninstall-hotkey: ## remove the Shift+Super+S binding
	$(ROOT)/skreenshot --uninstall-hotkey

deps: $(DEPS_RUN) ## install runtime deps (PyQt6) into a local .venv

$(DEPS_RUN):
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip -q install PyQt6 PyYAML
	touch $@

deps-dev: $(DEPS_DEV) ## install runtime + dev/test deps (pytest, ruff) into .venv

$(DEPS_DEV): $(DEPS_RUN)
	$(VENV)/bin/pip -q install pytest ruff
	touch $@

run: $(DEPS_RUN) ## run skreenshot from the checkout, verbose
	$(VENV_PY) $(ROOT)/skreenshot --verbose

test: $(DEPS_DEV) ## run the unit tests (no display needed)
	$(VENV_PY) -m pytest -m "not e2e and not e2e_wayland" -q

e2e: $(DEPS_DEV) ## run the X11 end-to-end smoke tests on a private Xvfb
	$(VENV_PY) -m pytest -m e2e -q

e2e-wayland: $(DEPS_DEV) ## run the Wayland end-to-end tests on a nested kwin_wayland
	$(VENV_PY) -m pytest -m e2e_wayland -q

lint: $(DEPS_DEV) ## lint the source with ruff
	$(VENV)/bin/ruff check src tests skreenshot

icons: ## re-render icon PNGs from the SVG source
	./icons/render.sh

clean: ## remove venv, caches and rendered test artifacts
	rm -rf $(VENV) .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
