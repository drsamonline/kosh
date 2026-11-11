# Privacy

Version: 1.0
Effective: 11 November 2026
Owner: Dr Sohil Momin — sohilmomin2000@gmail.com
Changes: Initial version.

## What this app is

Kosh — the file workbench of the CHCE family. A command-line tool and a local web
interface for splitting, compressing, archiving, verifying and inspecting files.

It runs on your own device. There is no account, no server and no sign-in.

## What it collects

**Nothing.** No analytics, no telemetry, no crash reporting, no fingerprinting, no
advertising identifier, no email capture.

There is no code in it that reaches a host outside your device: no request carries
anything you type or store.

## What is stored on your device

- The files you ask it to produce, where you ask it to put them.
- Backups (`.bak`) only when you pass `--backup`.
- The web interface writes uploads to `~/.kosh/sessions/<token>/` and deletes
  sessions older than six hours on the next upload. Nothing is sent anywhere; the
  browser talks to `127.0.0.1` and nothing else.

Clearing those directories erases them.

## Links that leave the app

Documentation, the project index and the support page contain ordinary links.
Opening one takes you to that site, with its own privacy policy; the app loads
nothing from it.

## Erasing

Delete the files you created, delete `~/.kosh/`, and uninstall the package
(`pip uninstall kosh`). There is nothing else to remove, because nothing else was
collected.

## Changes

A change to this policy will appear as a new version of this file with a new
effective date, in the repository's history.
