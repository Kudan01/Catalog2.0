# Catalog 2.0

**Development status:** Catalog 2.0 is currently under active development. It is not yet considered a stable release, and some functionality or installation procedures may still change.

Catalog 2.0 is a local media catalog for browsing photos, GIFs, videos, and folders stored on your own disk.

The application runs locally, keeps its catalog data separate from the source media, and does not require a cloud service.

## Features

- Folder tree navigation and folder preview cards
- Photo, GIF, and video previews
- Favorites
- Folder and media rename workflows
- Full-catalog and single-folder updates
- Thumbnail and preview cache
- English and Czech user interface
- Multiple visual themes
- Configurable gallery density and page sizes
- Separate catalog instances with their own database, settings, cache, and launcher

## Supported environment

Catalog 2.0 is currently intended and tested for Windows. Linux and macOS are not supported at this time.

Tested environment:

- Windows 10 and Windows 11
- Display resolutions: 2560 × 1440 and 1920 × 1080
- Browsers: Firefox, Google Chrome, and Microsoft Edge

## Requirements

- Python 3.11 or newer
- Python packages listed in `requirements.txt`
- FFmpeg and FFprobe available in `PATH` for video previews

Verify FFmpeg from PowerShell or Command Prompt:

```powershell
ffmpeg -version
ffprobe -version
```

## Installation

Clone or download the repository, open PowerShell in the project folder, and create a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a catalog instance:

```powershell
python catalog2.py setup-instance
```

The setup asks for the source media folder and proposes a separate catalog folder. You can accept the suggested destination or enter another absolute path.

Example:

```text
Source data folder: D:\Media\Photo_Archive
Catalog folder [D:\Media\Catalog2_Photo_Archive]
```

You can also provide both paths directly:

```powershell
python catalog2.py setup-instance `
  --data-root "D:\Media\Photo_Archive" `
  --catalog-root "C:\Catalogs\Catalog2_Photo_Archive"
```

The catalog folder must be outside the source data tree. Setup does not merge with or overwrite an existing catalog destination.

A created instance contains its own runtime data and a generated launcher:

```text
Catalog2_Photo_Archive\
  Catalog_Output\
    .venv\
    app\
  Start Catalog.bat
```

## Command help

Use the built-in CLI help to list all available options or inspect the setup and update commands:

```powershell
python catalog2.py --help
python catalog2.py setup-instance --help
python catalog2.py update-instance --help
```

The first command shows general Catalog 2.0 CLI help. The other two show options for creating a new instance and updating or migrating an existing instance.

## Starting the catalog

Start the generated instance with:

```text
Start Catalog.bat
```

The launcher starts the local Catalog 2.0 server and opens the catalog in your browser.

A fresh public copy must first be initialized with `setup-instance`; it does not contain a personal `config.json`.

## First use

After opening a new catalog instance:

1. Open **Settings**.
2. Run **Update catalog** to scan the source folder.
3. Use **Prepare all previews** to generate available folder and media previews.
4. Browse the catalog.

Photo tile thumbnails are generated on demand while browsing.

## Updating an existing instance

After downloading a newer project version, update an installed catalog from the new project folder:

```powershell
python catalog2.py update-instance --catalog-output "D:\Media\Catalog2_Photo_Archive\Catalog_Output"
```

The update replaces the runtime application files and updates the instance runtime environment while preserving configuration, database, cache, runtime state, and source media.

## Source media safety

Catalog 2.0 stores its database, settings, and generated previews separately from source media.

Normal browsing, scanning, preview generation, and catalog updates do not delete, move, or modify source media.

Rename actions are explicit operations initiated by the user.

## Supported media

Main media types currently include:

- Images: JPG, JPEG, JPE, JFIF, PNG, WEBP, BMP
- GIF: GIF
- Video: MP4, MOV, AVI, MKV, WEBM, FLV

Video preview generation requires FFmpeg and FFprobe.

## Project status

This repository is published primarily as a personal portfolio and reference project.

Catalog 2.0 was designed, tested, and iteratively developed by **Kudan01** with the assistance of generative AI tools.
