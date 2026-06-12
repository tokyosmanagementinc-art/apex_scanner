## Release v0.1.0

Artifacts included:
- `dist/apex_scanner-0.1.0-py3-none-any.whl` — Python wheel
- `dist/apex-scanner` — macOS single-file executable (arm64)

Notes
- Initial packaged release providing:
  - CLI entrypoint `apex-scanner` (`apex_scanner.main:main`)
  - Web dashboard (`apex-scanner dashboard` / Flask app)
  - Packaged templates under `apex_scanner/templates`

Install wheel locally:
```bash
pip install dist/apex_scanner-0.1.0-py3-none-any.whl
apex-scanner --help
```

Run standalone macOS binary:
```bash
chmod +x dist/apex-scanner
./dist/apex-scanner --help
```

Publishing
- A Git tag `v0.1.0` has been pushed. To publish a GitHub Release with attached artifacts, upload the `dist/` files to the release page or use `gh release create`.

Security
- Notarize and codesign the macOS binary before distributing broadly.
