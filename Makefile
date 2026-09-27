# Disk Usage — build, sign and install the app.

APP      := src-tauri/target/release/bundle/macos/Disk Usage.app

# The signing certificate is named in signing.local, which is untracked: the name of a
# keychain identity is local to the machine that holds it. Copy signing.local.example and
# put your own self-signed certificate's name in it. See the README.
-include signing.local
export APPLE_SIGNING_IDENTITY
SIGN_ID  := $(APPLE_SIGNING_IDENTITY)


.PHONY: check test app install-app dmg dev icons screenshots sign-check

## cargo test + TypeScript type-check.
check:
	cd src-tauri && cargo test --lib
	npx tsc --noEmit -p tsconfig.json

test: check

# Release builds strip the builder's home directory out of the binary. Rust bakes absolute
# paths (~/.cargo/registry, ~/.rustup) into panic metadata, which would otherwise ship the
# builder's username to everyone who downloads the app. Debug builds skip this so `make dev`
# and `make check` keep their incremental caches.
RELEASE_RUSTFLAGS := --remap-path-prefix=$(HOME)=/build

## Build the signed .app. The identity comes from signing.local; see the README for why a
## stable signature matters (Full Disk Access is tied to it).
app:
	@test -n "$(SIGN_ID)" || { echo "APPLE_SIGNING_IDENTITY is not set: copy signing.local.example to signing.local"; exit 1; }
	RUSTFLAGS="$(RELEASE_RUSTFLAGS)" npm run tauri build
	@codesign -dv --verbose=2 "$(APP)" 2>&1 | grep -E "^Authority=$(SIGN_ID)" >/dev/null \
	  && echo "signed with $(SIGN_ID)" \
	  || { echo "WARNING: app is not signed with $(SIGN_ID); privacy grants will not persist"; exit 1; }

## Build and replace /Applications/Disk Usage.app (same signature, so Full Disk Access persists).
install-app: app
	@pkill -x DiskUsage 2>/dev/null || true
	@rm -rf "/Applications/Disk Usage.app"
	@ditto "$(APP)" "/Applications/Disk Usage.app"
	@echo "installed /Applications/Disk Usage.app"

## Build the app and pack it into dist-release/Disk-Usage.dmg for a GitHub release, laid out
## like other Mac installers (tools/dmg/). Finder lays out the window, so the first run asks to
## let the terminal control Finder.
dmg: app
	@python3 tools/dmg/make_dmg.py

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
