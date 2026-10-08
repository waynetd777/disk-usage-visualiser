# Disk Usage

Disk Usage shows what is using the disk: every folder as a block inside its parent's block, sized by what it takes up, so the big things are big. This guide is also the app's help: press `?` or the ? button in the title bar.

## The map

Every folder is drawn nested inside its parent, with its name and size where the block is big enough. Sizes are **allocated bytes on disk**, not file lengths, because the question is what is using the disk.

- The files sitting directly in a folder are one grey dashed block, labelled with their count.
- Folders too small to draw at the current zoom are folded into a **more folders** block, so a folder's area is always its total.
- **Colour is nesting level**, in rainbow order from the scan root: red, orange, amber, green, teal, blue, indigo, violet. The level is counted from the root, so a folder keeps its colour as you zoom.
- Hover a block for its full path, size on disk, item count and share of its parent.

## Getting around

- **Click a folder block to zoom in.** Clicking its grey loose-files block opens the same folder.
- The **breadcrumb** at the top names where you are; click an earlier name to go back up. **Esc** or **⌘↑** go up one level.
- The **‹ ›** buttons (⌘[ and ⌘]) step back and forward through where you have been, like Finder.
- **Choose a folder** in the sidebar (⌘O) scans a different folder; the five most recent roots sit below it. The default is the whole boot volume.

## Finder and the right-click menu

- **Reveal in Finder**: the arrow on a block's header, ⌘-click on the block, or the arrow beside the breadcrumb for the folder on screen (⌘⇧R).
- **Right-click** a block for a menu: Reveal in Finder, Zoom in, Copy path, Get Info.

## Files in a folder

When a folder holds files directly, a files box lists them, and it fills the map area when the folder contains only files. The table shows Name, Date Modified, Size, On Disk and Kind, like Finder.

- Type in the box at the top to filter by name. It covers the files directly in this folder, not subfolders.
- Click a column heading to sort; click again to reverse.
- Double-click a file, or press its arrow, to reveal it in Finder.
- A folder with thousands of files loads a page at a time as you scroll.
- **Size** is the file's length; **On Disk** is the space it takes. Listing files never reads their contents or downloads online-only cloud files.

## Free space

At the top of a whole-disk scan (the boot volume, or a disk under /Volumes) the disk's free space is drawn as a dotted green block beside the folders, so the map shows the whole disk. The **Free space** switch in the title bar hides it, and the app remembers the choice. It is not drawn inside a folder, or for a scan of a single folder.

The sidebar's used figure comes from the filesystem and is larger than the scan total: it includes local Time Machine snapshots, purgeable space, swap, the recovery volume and folders the scan skips or cannot read. Hover the bar for the breakdown.

## Cloud folders

OneDrive, iCloud Drive and anything else under Library/CloudStorage or Library/Mobile Documents carry a double edge and show two numbers: what is on disk and what is in the cloud. A file that is online-only has a length but takes no space, so it counts as nothing on disk and appears only in the folder's "in cloud" figure.

So that an online-only cloud folder does not vanish from the map, its block is never drawn smaller than 2% of its cloud size. Inside such a folder the blocks are proportional to cloud size, and the tooltip says so.

## Scanning

**Reload** (⌘R) re-scans the whole tree. While a scan runs the button reads **Stop**; stopping keeps the last completed scan on screen. There is no pause.

The scan is a parallel walk across every core; a full boot volume of about a million items takes on the order of half a minute on an SSD. It does not follow symlinks, counts a file with several hard links once, and does not descend into /System/Volumes, /Volumes, /dev or the virtual folders.

Every completed scan is **cached**. On launch the cached map is drawn at once and a fresh scan runs behind it; the strip under the title bar shows the folder being walked, the items counted so far and a percentage measured against the previous scan. The title bar says when the scan on screen finished.

## Full Disk Access

Folders the app is not allowed to read are skipped and counted. If any were skipped and the app does not have Full Disk Access, a banner says so and opens System Settings. Grant it under Privacy & Security › Full Disk Access, then Reload, and Mail, Safari, Photos and the other protected folders are included.

Some folders stay unreadable even then: system folders owned by root, such as /private/var/folders. Full Disk Access opens your own protected folders, not those. The sidebar shows how many were skipped.

## Ask

**Ask** (⌘K) opens a chat about the folder on screen. It puts the scan to an AI: the folder you are looking at and its biggest folders, with sizes, and asks where space can be reclaimed safely. Start with one of the suggested questions, or type your own; follow-ups carry on the same conversation.

- Ask runs through an AI command-line tool already installed and signed in on this Mac: Claude Code, Codex, Antigravity or GitHub Copilot. No account or key is set up in the app. If none is installed the Ask button is disabled and says so.
- The model menu in the panel's header lists every model those tools offer, grouped by tool. Claude Opus is the default; the choice is remembered.
- The AI sees sizes, names and paths only. It cannot read your files, and the app deletes nothing: it advises, and you act in Finder or Terminal.
- **Copy** under an answer copies it to the clipboard as text and as formatted text, for pasting into a note or a message.
- **New chat** starts over about what is on screen now. Zooming to another folder and asking a follow-up sends the new folder with the question.

## Menu bar

Closing the window hides it; the app stays in the menu bar. The menu-bar item shows the volume's used and total space, the scan's progress while one runs, and offers Open Disk Usage, Rescan and Quit.

## Keyboard shortcuts

| Keys | What they do |
|---|---|
| ⌘R | Re-scan the whole tree |
| ⌘O | Choose a folder to scan |
| Esc or ⌘↑ | Up one level (Esc first closes an open menu or panel) |
| ⌘[ and ⌘] | Back and forward through where you have been |
| ⌘⇧R | Reveal the folder on screen in Finder |
| ⌘-click a block | Reveal that folder in Finder |
| ⌘K | Ask about the folder on screen |
| ? | This help |
