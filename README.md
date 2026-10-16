# ISAPI Studio

Local app for configuring Hikvision cameras over ISAPI. One Python file, or a single Windows exe.

- **Camera scanner:** finds Hikvision devices on your network (or a range you give it) and fills in the connection form.
- **Motion zones:** paint or polygon-draw the detection grid on a live snapshot (grid size is read from the camera).
- **Settings:** generic editor for common ISAPI pages (time, network, image, OSD, streams, ...) with a live preview. Add a page by adding a line to `SECTIONS` in `main.py`.
- **ISAPI console:** send any GET/PUT/POST/DELETE to an `/ISAPI/...` path.

Every write saves a backup of the previous XML next to where you run the tool.

## Usage

```
pip install -r requirements.txt
python main.py
```

It opens http://localhost:8765. Scan for cameras or type an IP, enter the camera login, and connect.
You can also connect from the command line: `python main.py --ip 192.168.1.64 --user admin`.
Other options: `--camport`, `--https`, `--channel`, `--port`. The page loads Bootstrap and jQuery from a CDN.

## Build the exe

```
pip install pyinstaller
pyinstaller --onefile --noconsole --name "ISAPI Studio" main.py
```

The result is `dist/ISAPI Studio.exe`. Use the Quit button in the page to stop it. Windows SmartScreen may warn about unsigned builds.

## Security notes

- The scanner only probes the /24 around your PC (or a range up to /22 you type in) and only for the ISAPI device info path.
- The server binds to `127.0.0.1` only and talks to the single camera you pass in. Don't expose it to a network: it has no login of its own and holds the camera password in memory.
- Requests must come from localhost with an `X-Tool` header, which blocks other websites from driving it. This is not authentication.
- The ISAPI console can do anything ISAPI allows, including rebooting or resetting the camera. Use it on cameras you own.
- Use only on devices you are authorised to configure.

## Status

Developed against Hikvision cameras with the `motionDetection` grid (`gridMap`) format. Other firmware may differ; issues and PRs welcome (a tested camera model list would be great).

## Releases

Pushing a tag like `v1.0.0` builds `ISAPI Studio.exe` with GitHub Actions and attaches it to a release.

## Contributing

Issues and pull requests are welcome. Keep it a single dependency-light file where you can.

## License

MIT
