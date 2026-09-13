# Disk Usage — build, sign and install the app. Same shape as backup-manager's Makefile.

APP      := src-tauri/target/release/bundle/macos/Disk Usage.app
SIGN_ID  := $(shell /usr/bin/plutil -extract bundle.macOS.signingIdentity raw -o - src-tauri/tauri.conf.json)

.PHONY: check test app install-app dev icons screenshots sign-check

## cargo test + TypeScript type-check.
check:
	cd src-tauri && cargo test --lib
	npx tsc --noEmit -p tsconfig.json

test: check

## Build the signed .app. The identity comes from tauri.conf.json; see the README for why a
## stable signature matters (Full Disk Access is tied to it).
app:
	npm run tauri build
	@codesign -dv --verbose=2 "$(APP)" 2>&1 | grep -E "^Authority=$(SIGN_ID)" >/dev/null \
	  && echo "signed with $(SIGN_ID)" \
	  || { echo "WARNING: app is not signed with $(SIGN_ID); privacy grants will not persist"; exit 1; }

## Build and replace /Applications/Disk Usage.app (same signature, so Full Disk Access persists).
install-app: app
	@pkill -x DiskUsage 2>/dev/null || true
	@rm -rf "/Applications/Disk Usage.app"
	@ditto "$(APP)" "/Applications/Disk Usage.app"
	@echo "installed /Applications/Disk Usage.app"

## Redraw design/icon.png and the tray template, then regenerate the Tauri icon set.
icons:
	@python3 tools/make_icons.py
	@npx tauri icon design/icon.png >/dev/null
	@rm -rf src-tauri/icons/android src-tauri/icons/ios
	@echo "regenerated src-tauri/icons"

## Recapture docs/screenshots/app.png from the installed app (needs Screen Recording on this terminal).
screenshots:
	@tools/screenshot.sh

sign-check:
	@codesign -dv --verbose=2 "/Applications/Disk Usage.app" 2>&1 | grep -E "^(Identifier|Authority|Signature|TeamIdentifier)"

dev:
	npm run tauri dev
