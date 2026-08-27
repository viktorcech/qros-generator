# QROS Generator

Turns Atari 8-bit executables (**XEX / COM / OBX**) into **QROS / EMO** turbo tape images — `.cas` for emulators and tape emulators, `.wav` for recording onto a real cassette.

One file, two front ends: run it with no arguments for the GUI, or pass a filename for the command line.

<p align="center">
  <img src="qros-generator.png" width="80%" alt="QROS Generator">
</p>

## What it does

- **CAS and WAV.** The CAS output is bit-for-bit identical to v0.4, checked against a reference `Decathlon.cas`.
- **Manchester (biphase-L) WAV encoder** with a phase accumulator, so a non-integer samples-per-bit ratio (96000/6595 = 14.5565) never drifts across a whole tape. 44.1 / 48 / 96 / 192 kHz, adjustable amplitude, optional inverted polarity for recorders that flip the signal.
- **Bootable tapes** (`--boot`). The compiled QROS loader is written ahead of the data as standard cassette boot records, with the tape's baud divisor and total block count patched into it — so the tape shows a progress percentage and starts on its own (START on power-up).
- **Verifies its own output.** Every CAS is decoded back to a payload and compared against the source; `--verify-wav` re-decodes the audio too.
- **Batch conversion** of many files at once, and `--rename` to change the on-tape name inside a finished CAS without regenerating it.
- **Streamed WAV writing** — the recording is not held in memory, so a large XEX doesn't blow up RAM.

## Tape format

A QROS record is 132 bytes: a 2-byte block index, a type byte, 128 data bytes and an SIO checksum. It is the standard Atari cassette record with the two `$55` sync bytes replaced by the index.

| Type | Meaning |
|------|---------|
| `$FC` | complete data block (128 bytes) |
| `$FA` | partial block (last one, byte count in the final position) |
| `$FD` | header block — carries the 25-character program name at offset `$08` |
| `$FE` | end of file |
| `$FF` | blank setup block (optional, `--blank`) |

Inter-record gaps: 1258 ms before the first record, 39 ms everywhere else. Speeds on offer:

| Baud | |
|------|---|
| 6595 | empirical, verified with a PAL Atari (default) |
| 6617 / 6678 | exact POKEY divisor 127, PAL / NTSC |
| 9600 | standard value |
| 9535 / 9622 | exact POKEY divisor 86, PAL / NTSC |

POKEY derives its rate as `baud = clock / (2 × (divisor + 7))`.

## Usage

```
python qros_generator-v0.5.py                          # GUI
python qros_generator-v0.5.py game.xex                 # -> game.cas
python qros_generator-v0.5.py game.xex --wav -b 9600   # CAS + WAV at 9600
python qros_generator-v0.5.py game.xex --boot -n "MY GAME"
python qros_generator-v0.5.py *.xex                    # batch
python qros_generator-v0.5.py game.cas --rename "NEW NAME"
python qros_generator-v0.5.py game.xex --info          # just analyse, don't convert
```

Useful flags: `-o` output file, `-n` tape name (max 25 ATASCII characters), `-b` baud, `--wav` / `--wav-only`, `--rate` and `--volume` for the audio, `--invert`, `--blank`, `--boot`, `--no-verify`, `--verify-wav`.

## Requirements

**Python 3** and nothing else — `tkinter` is only needed for the GUI, and the command line still works without it.

For `--boot` you also need `qrosload.bin`, the assembled QROS loader, sitting next to the script. Build it from **[viktorcech/qros-loader](https://github.com/viktorcech/qros-loader)**.

## Credits

- **QROS system & software** — Ing. Matúš Žúbor
- **QROS hardware revival (2025)** — Aleister
- **QROS Generator** — W1K
- **Reference code** — Turgen System (baktra)
