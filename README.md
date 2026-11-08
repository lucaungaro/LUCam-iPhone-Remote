# LUCam iPhone Remote

Control several iPhones running **Blackmagic Camera** (app 3.4 or later) from a
web page, through the app's REST API (`/control/api/v1`).

- Add cameras by IP/hostname, or find them with Bonjour discovery or a subnet scan
- Per camera: Lens, Iris, ISO, FPS, Shutter, White Balance, Tint, Camera letter, Reel,
  and recording Format (codec, resolution, colour space)
- **Global row**: one value is pushed to every *armed* camera and overrides its own value
- REC / STOP per camera, plus REC ALL / STOP ALL (fired in parallel so all cameras start together)
- Live state: tally, timecode, next clip name, errors
- Live view and Multiview of the camera feeds in the browser
- Works from any browser on the network, including iPads: `http://<this Mac's IP>:9980`

```
install.sh         installer for macOS (see below)
server.py          the controller: web server + camera engine
python/lucam/      camera client, discovery, manager, live view (stdlib only)
web/               control panel (index.html) and live view (live.html)
tools/             mock_camera.py: simulated cameras for testing
cameras.json       your camera list (created automatically, not in git)
```

## 1. Install (on each Mac)

Copy or clone this folder onto the Mac, then:

```bash
./install.sh
```

The installer installs only what is missing and asks before installing
Homebrew:

- **Homebrew:** reused if present (`/opt/homebrew` or `/usr/local`). Otherwise
  it offers to install it. It's only needed for MediaMTX, so you can decline,
  and everything except the live view still works.
- **Python 3.8+:** Homebrew's Python if present, otherwise the system one when
  the Command Line Tools are installed. Otherwise it installs Python with
  Homebrew.
- **MediaMTX:** installed with Homebrew if missing. It's only used by the live
  view.
- **Launchers:**
  - a `lucam-remote` command, in Homebrew's `bin` (or `~/.local/bin` without
    Homebrew);
  - **LUCam iPhone Remote.command** in this folder: double-click it to start and open
    the panel, or drag it to the Dock.

Options: `--yes` (don't ask), `--uninstall` (removes the launchers only). Re-run
the installer after moving the folder, because the launchers point at it.

On first run, macOS asks to let Terminal (or Python) find devices on the
**Local Network**. Click **Allow**, or cameras won't be found. If you missed
it, turn it on under *System Settings → Privacy & Security → Local Network*.

Without the installer: `python3 server.py --open` (needs Python 3.8+, plus
MediaMTX on the PATH for the live view).

## 2. Prepare each iPhone

1. Update Blackmagic Camera to 3.4 or later (REST API support arrived in 3.4).
2. In the app's settings, turn on **HTTP Access**. This is not the "Remote Camera"
   mode, which uses Blackmagic's private iPhone-to-iPhone protocol. With HTTP
   Access on, the app serves the REST API over **HTTPS on port 4444** with a
   self-signed certificate, and reads and writes need no password (tested with
   app 3.5.3 on an iPhone 15 Pro). Typing just the IP in the UI tries
   `https://<ip>:4444` first, then plain HTTP on port 80.
3. Keep the app open: HTTP Access stops while the app is in the background.
4. Put the iPhones and the Mac on the same network. Wired Ethernet through a
   USB-C adapter is the most reliable on set.
5. Give each phone a fixed IP (a DHCP reservation on the router), so saved
   cameras reconnect after a reboot.

## 3. Run

```bash
lucam-remote --open
```

Or double-click **LUCam iPhone Remote.command**. The panel is at
http://localhost:9980. Options: `--port 9980`, `--config path/to/cameras.json`,
`--log-dir path/to/logs`. Quit with Ctrl-C or by closing the Terminal window.
Quitting stops the live view and restores any camera that is in live mode.

### Log files

Each run writes two files to `logs/` (next to `server.py`, or `--log-dir`):

- `lucam-YYYYMMDD-HHMMSS.log`: what the panel's log shows (cameras found, values
  changed or restored, errors), plus every value written to a camera and every
  request that failed, with the time to the millisecond.
- `lucam-YYYYMMDD-HHMMSS.requests.csv`: every request sent to every camera, with
  the time it was sent, how long the camera took to answer and the status. Laid
  over a clip's timecode this tells what the app asked a phone at any moment. At
  16 cameras it grows by about 25 MB an hour; `--no-request-log` skips it.

`--no-log` keeps the log in memory only, as before.

## 4. Using the UI

- **Arm** (checkbox on each row): only armed cameras receive global values and REC ALL.
- Each value field takes typing + Enter, ↑/↓ to step (Shift ×10), or the ▾ list of values the camera reports as supported.
- An amber value is still being sent. If the camera rejects it, the field reverts and the error shows under the camera name.
- An out-of-range value is replaced by the closest one the camera can take: White Balance, Tint, Iris and Reel are clamped to the camera's range (2000 K becomes 2500 K on an iPhone), while ISO, Shutter, FPS and Lens pick the nearest value the camera lists (ISO 3000 becomes 3200, 30 mm picks the 24 mm lens). The field shows the value that was sent. In the global row each camera gets the closest value for itself.
- On/off switches (autofocus, focus lock, LUT, clean feed) flip as soon as you click and pulse until the camera confirms, then flash green. If the command fails or isn't confirmed within 6 seconds, the switch shakes and goes back.
- Values set from here stay locked, so the phone's automatic modes can't change them (focus has its own lock, below):
  - **ISO, Shutter, Iris** turn the app's auto exposure off.
  - **Lens** turns automatic lens switching off, on phones that have it.
  - **White Balance, Tint**: the API has no auto-WB switch, so the controller
    holds the value (a mark on the field's left edge) and puts it back if the
    phone changes it, within about 3 seconds. **Auto white balance** (⋯ menu, or
    AWB in the global row) or applying a preset releases the hold. The hold is
    saved in `cameras.json`.
- **▶** before a camera's name expands its row with focus controls (the
  browser remembers which rows are open):
  - **Focus** slider, 0–100 % on the phone's own scale. Moving it turns
    continuous autofocus off; the phone does this itself.
  - **Autofocus** switch (continuous on/off) and **Focus once**.
  - **Lock focus**: turns autofocus off, refuses every focus change from here,
    and holds the focus position: if anything on the phone moves it (tap to
    focus, an autofocus pass), the controller puts it back within half a
    second. The app's own focus lock isn't in its API, so the controller
    holds it, saved in `cameras.json`. A tap still makes the phone refocus for
    about a second before it's put back: to stop that, also press the lock
    under the focus slider in the app. The slider here keeps working while
    the app's lock is on.
  - **Presets**: the camera's presets (active one marked), with **⬇ Download**
    (saves the `.cset` file), **Apply**, **Delete**, **Save current…** (saves
    the camera's current settings as a new preset) and **⬆ Upload…**
    (`.cset` or `.bmcpreset`; the camera adds -1, -2… if the name is taken).
    Presets also store focus, LUT selection and many other app settings, so
    applying one can change more than exposure and colour.
  - **LUT**: display LUT on/off for each output that has one: **Screen** (the
    phone's display) and **HDMI**. It only affects monitoring.
  - **HDMI video › Clean feed**: the HDMI picture without the app's overlays.
    Switching it on or off also sets the app's HDMI output to **Video feed**
    (instead of Mirror display). The API has no setting for that, so the
    controller applies a copy of the phone's current settings with that one
    option changed, through a temporary preset it deletes straight after.
    Not possible while recording.
- **↻** reconnects a camera from scratch.
- Cameras are listed by camera letter (A, B, C…), here and in Multiview. Cameras whose letter isn't known yet (connecting, offline) come last.
- **⋯** menu: auto white balance, open/stop live view, reconnect, remove.

## Network interfaces

With several network interfaces (Wi-Fi, Ethernet adapters, VPN), use the
**Network** section at the bottom of the control panel:

- **Auto** (default): discovery searches every network except VPNs. Each
  camera streams its live view to the address the Mac's routing table uses to
  reach it.
- **A specific interface** (for example "Ethernet Adapter (en5) · en5 ·
  10.0.0.2/24"): discovery searches only that network, with the subnet scan
  covering its real subnet, at most a /22. Cameras on that network stream their
  live view to that interface's address. Cameras elsewhere keep using the
  routed address.

The section also shows the control-panel address to open from other devices,
and which interface each camera is reached through. A camera on a different
interface than the selected one is flagged. The choice is saved in
`cameras.json`. If the selected interface disappears (adapter unplugged),
discovery reports it until it is back or you pick another one; cameras you
already added keep working through the routing table. Interface names come
from macOS (*System Settings → Network*); on other systems the section lists
plain addresses.

## Live view (video feed)

⋯ → **Open live view** on a camera opens a page
with that camera's live picture. **Multiview** in the header shows every armed
camera in a grid, with tally, slate and timecode, plus REC and live start/stop
per tile. Its top bar matches the control panel's, with **REC ALL** and
**STOP ALL**. These pages also work from an iPad on the same network.

How it works: the controller uploads a custom streaming platform called
"LUCam iPhone Remote RTMP" to the phone, pointing at this Mac. It then starts the
phone's live stream, which goes to [MediaMTX](https://mediamtx.org) running
locally. MediaMTX passes it on to the browser over WebRTC without re-encoding,
with roughly 0.2–0.5 s of latency.

```bash
brew install mediamtx
```

The controller starts MediaMTX itself and stops it on exit. It uses ports 1935
(RTMP), 8889 (WebRTC page) and 8189/UDP (WebRTC media).

**Phone settings the stream needs.** Tested on an iPhone 15 Pro with app 3.5.3:

- **Recording codec must be H.264.** The app's live stream reuses the
  recording encoder, and RTMP only carries H.264. With ProRes (any flavour,
  even Proxy at 1080p) or HEVC, the phone connects but sends no video, and the
  app shows 0 MB/s. Resolution doesn't matter: H.264 streams fine in HD and 4K.
  The controller warns on the camera row when a stream sends no video.
- **Vertical (portrait) recording works.** In vertical mode the phone accepts
  the switch to H.264 but keeps its resolution (for example 1214×2160), so the
  live stream is vertical. Format-panel resolution changes are also ignored by
  the phone in vertical mode; the controller reports that as an error.
- **A whole-number frame rate** (24, 25, 30, 48, 50 or 60). The app rejects
  stream profiles for 23.98, 29.97 and 59.94.
- **Remote Camera off** was also set during testing. It wasn't the cause, but
  it hasn't been tested on.

Starting the live view switches the phone's active streaming platform away
from whatever was set before, such as Blackmagic Cloud.

**Live mode switches the recording format automatically.** Opening a live view
saves the camera's real recording format and switches it to H.264 1080p. The
frame rate is kept, rounded to a whole number (23.98 → 24). The real format is
restored when:

- the live view is stopped (⋯ → Stop live view, Stop live in the viewer, or
  **■ Stop all lives** in the control panel or Multiview header, which ends
  every live view at once, armed or not);
- REC is pressed on **any** camera, or REC ALL. Every camera in live view is
  restored first, and only then do the armed cameras start recording. A camera
  whose format can't be restored is not recorded, and shows an error instead;
- the controller quits.

The saved format is kept in `cameras.json`, so a crash can't leave a phone on
the live format: restart the controller and stop the live view. While a camera
is live, its Format, FPS and other format changes edit the saved format, which
is applied when it is restored.

## Recording format

Each camera row has a **Format** cell (for example "ProRes 422 HQ · 4K ·
Film"). Click it to choose the **resolution**, **codec** and **colour space**
from the lists the phone reports for its model. FPS has its own column. Format
changes are per camera; there is no global format.

The app calls colour space "dynamic range": `/system/dynamicRange`, with
options from `/system/supportedDynamicRanges`. These aren't in Blackmagic's
REST document. An iPhone 15 Pro offers Video, Extended Video, Film and HLG.
All four stream fine with H.264, so live mode leaves colour space alone. It
still saves and restores it with the rest of the format.

## Testing without iPhones

```bash
python3 tools/mock_camera.py --count 3 --base-port 8081 --bonjour
```

```bash
python3 server.py --config /tmp/test-cameras.json --open
```

The first command starts three simulated cameras (registered with Bonjour). The
second runs the controller with a separate camera list. Then add the mocks
through Discover. The mocks don't stream video.

## Notes from real-iPhone testing

Tested on an iPhone 15 Pro with app 3.5.3:

- **Discovery.** Bonjour advertises the API as `_http._tcp`, but the app
  actually serves it over HTTPS, so discovery tries both.
- **Slate fields.** Camera letter is the slate's `project.camera` field, and
  reel is `clip.reel`.
- **Lens.** The iPhone app exposes its physical lenses at `/lens/cameras` (not
  in Blackmagic's REST document). The Lens dropdown lists them with the phone's
  own zoom labels, for example 13 mm 0.5×, 24 mm 1×, 48 mm 2×, 77 mm 3× and
  Front on a 15 Pro. Picking one switches the lens with
  `PUT /lens/cameras/active {"id": "Lens77mm"}`. Typing a focal length, or
  setting it in the global row, selects the back lens with that focal length,
  so one value works across different iPhone models. Lens IDs aren't
  reliable names (an iPhone 17 Pro Max reports its 100 mm 4× lens as
  `Lens77mm`), so the list goes by the phone's focal length and zoom factor.
  Phones with two front lenses get distinct names such as "Front ultra wide"
  and "Front". For cameras without a
  lens list, Lens falls back to `/lens/zoom` and the per-model table
  `NATIVE_LENSES_MM` in `python/lucam/manager.py`.
- **Iris.** The Iris column offers third-stop f-numbers within the range
  from `/lens/iris/description`. A value can also be typed ("2.8", "f/2.8").
  iPhones with a fixed aperture (an iPhone 15 Pro and a 17 Pro Max were tested)
  report `controllable: false`, so their Iris shows the f-number (f/1.78)
  greyed out. The global Iris only goes to armed cameras whose iris is
  adjustable. Adjustable iris is so far tested only on the simulator (camera
  A of `tools/mock_camera.py`), not on a real iPhone.
- **Focus.** `/lens/focus` (normalised 0–1) and `PUT /lens/focus/doAutoFocus`
  are documented. Continuous autofocus is the undocumented
  `/lens/focus/autoFocus`: `{"enabled": true, "mode": "Continuous"}` for on,
  `{"mode": "OneShot"}` for off. Other modes are rejected.
- **Presets.** `/presets` lists names without extension; download, apply and
  delete use that name; uploads need a `Content-Disposition` filename ending in
  `.cset` or `.bmcpreset`. A preset is a JSON file of the app's settings. The
  phone serves its own API reference at
  `https://<phone>:4444/control/documentation.html`.
- **LUTs can't be transferred.** The app's API has no LUT file endpoints, only
  a display-LUT on/off switch per display (`/monitoring/{display}/displayLUT`;
  the phone lists `Device` in `/monitoring/display`, and `HDMI` works too). Presets name the
  selected LUT (`lutSelection`) but don't contain the LUT file.
- **Unsupported controls.** Any endpoint the app doesn't implement greys out
  that control on the row instead of failing.
