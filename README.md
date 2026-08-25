# Skylanders SuperChargers Racing Audio Tool

A community modding tool for **Skylanders SuperChargers Racing** that makes streamed audio modding easier.

The tool provides a simple graphical interface for extracting, identifying, organizing, and replacing audio stored in `Streams2.dat`.

## Features

- Extract streamed audio from `Streams2.dat` to WAV.
- Generate a stream manifest containing Media IDs and stream information.
- Scan the game's PKZ SoundBanks to associate Media IDs with banks / characters / contexts.
- Organize extracted WAV files into useful folders.
- Replace an existing streamed audio file with a completely different WAV.
- Automatically match the required sample rate and channel count.
- Encode replacement audio to Wii DSP-ADPCM.
- Update Wwise duration metadata so replacement audio can be longer than the original.
- Repack `Streams2.dat` without modifying the original source file.
- Chain multiple audio replacements in the same modified `Streams2.dat`.

## Requirements

- **Windows**
- **Python 3.10 or newer** recommended
- **Tkinter** for the graphical interface

No third-party Python packages are required. The tool uses only the Python standard library.

If Python was installed from the official Windows installer at python.org, Tkinter is normally included automatically.

You can still include the provided `requirements.txt` in the repository, but `pip install -r requirements.txt` does not need to install anything.

## Preparing the Game Files

Before using the tool, you need to extract the game files from your **Skylanders SuperChargers Racing Wii ISO**.

A recommended method is to use **Wiimms ISO Tools (WIT)** to extract the ISO contents.

## Usage

Run:

    python skylanders_audio_tool_v2.py

The program contains two main tabs.

### Extract & Organize

1. Select the game's `Data` folder containing `Streams2.dat` and the PKZ files.
2. Select an output folder.
3. Click **EXTRACT & ORGANIZE AUDIO**.
4. Wait for extraction and SoundBank analysis to finish.

The output includes the extracted WAV files, `streams_manifest.csv`, and `stream_names.csv`.

### Replace Audio

1. Select your `Streams2.dat`.
2. Select the extracted WAV you want to replace. Its filename contains the Media ID used by the tool.
3. Select your replacement WAV.
4. Click **CREATE MODIFIED STREAMS2.DAT**.
5. Test the generated file in-game.

After a successful replacement, the newly modified Streams2 file automatically becomes the source for the next replacement, allowing multiple audio edits to be applied one after another.

## Credits / Special Thanks

This project would have been much harder to develop without the existing work and research of the Skylanders modding community.

Special thanks to **maff** from the **Skylanders Reverse Engineering Discord server**. Their **Cogwheel** tool was extremely helpful for understanding and inspecting the game's PKZ resource structure, including chunks, resource data, and the way game assets are stored.

Special thanks as well to **Haz** for the **Skylanders SuperChargers Racing Extractor**, shared in the **Beenox Goliath Modding server**. That extractor and its approach to the game's files were very useful references while investigating SuperChargers Racing's data and building this audio workflow.

Thanks to everyone in the Skylanders reverse-engineering server and modding communities who has documented the game formats, experimented with the files, and shared their findings.

## Disclaimer

This is an unofficial community-made modding tool and is not affiliated with or endorsed by Activision, Toys for Bob, Beenox, Nintendo, or Audiokinetic.

Always keep backups of the original game files before modifying anything.
