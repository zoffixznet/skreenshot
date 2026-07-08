# skreenshot: the front door for installing, running and testing.
# `make` or `make help` lists the targets.

SHELL := /bin/sh
PYTHON ?= python3
PREFIX ?= $(HOME)/.local
BINDIR := $(PREFIX)/bin
ICONDIR := $(PREFIX)/share/icons/hicolor
ROOT := $(abspath .)
VENV := .venv

.DEFAULT_GOAL := help

.PHONY: help install uninstall install-hotkey uninstall-hotkey run test e2e lint icons clean

help: ## list all targets with what they do
	@echo "skreenshot make targets:"
	@awk -F':.*## ' '/^[a-z0-9-]+:.*## / { printf "  %-18s %s\n", $$1, $$2 }' Makefile

install: ## symlink skreenshot into ~/.local/bin and install icons
	mkdir -p $(BINDIR)
	ln -sfn $(ROOT)/skreenshot $(BINDIR)/skreenshot
	for size in 48 128 256; do \
		mkdir -p $(ICONDIR)/$${size}x$${size}/apps; \
		cp icons/skreenshot-$${size}.png $(ICONDIR)/$${size}x$${size}/apps/skreenshot.png; \
	done
	@echo "installed: $(BINDIR)/skreenshot (make sure $(BINDIR) is on PATH)"

uninstall: ## remove the ~/.local/bin symlink and icons
	rm -f $(BINDIR)/skreenshot
	for size in 48 128 256; do \
		rm -f $(ICONDIR)/$${size}x$${size}/apps/skreenshot.png; \
	done
	@echo "uninstalled (hotkey binding, if any, is removed by uninstall-hotkey)"

install-hotkey: ## bind Shift+Super+S to skreenshot (XFCE or KDE)
	$(ROOT)/skreenshot --install-hotkey

uninstall-hotkey: ## remove the Shift+Super+S binding
	$(ROOT)/skreenshot --uninstall-hotkey

run: ## run skreenshot from the checkout, verbose
	$(ROOT)/skreenshot --verbose

test: ## run the unit tests (no display needed)
	$(PYTHON) -m pytest -m "not e2e" -q

e2e: ## run the end-to-end smoke tests on a private Xvfb
	$(PYTHON) -m pytest -m e2e -q

lint: $(VENV)/bin/ruff ## lint the source with ruff (bootstraps a venv)
	$(VENV)/bin/ruff check src tests skreenshot

$(VENV)/bin/ruff:
	$(PYTHON) -m venv --system-site-packages $(VENV)
	$(VENV)/bin/pip -q install ruff

icons: ## re-render icon PNGs from the SVG source
	./icons/render.sh

clean: ## remove venv, caches and rendered test artifacts
	rm -rf $(VENV) .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
