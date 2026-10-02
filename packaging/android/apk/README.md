# Rotating Proxy Installer (Android)

A minimal installer/bootstrap app for Rotating Proxy. When opened after installing
the APK it checks whether [Termux](https://f-droid.org/packages/com.termux/) is
installed:

- **Termux missing** — shows a short explanation and buttons to install Termux
  (F-Droid page, with a GitHub releases fallback link).
- **Termux present** — immediately asks Termux's `RunCommandService` to run
  `curl -fsSL <release script URL> | bash` in a **foreground** session
  (`RUN_COMMAND_BACKGROUND=false`, `RUN_COMMAND_SESSION_ACTION="0"`), so the
  install script's output is visible live in a new Termux session, which opens
  automatically. The download/install starts the Python proxy.

Before a fresh install works, two manual Termux-side steps are required (the app
shows these with an "Open app settings" shortcut when needed):

1. Termux → Settings → enable **"Allow external apps"**
   (`allow-external-apps = true` in `~/.termux/termux.properties`).
2. Android Settings → Apps → Rotating Proxy Installer → Permissions →
   **Additional permissions** → allow **"Run commands in Termux environment"**
   (the `com.termux.permission.RUN_COMMAND` permission is user-granted, not
   auto-granted at install).

The app has no external dependencies: plain Java, `android.app.Activity`, UI built
entirely in code (no `res/` directory).

The script URL comes from `BuildConfig.RELEASE_SCRIPT_URL`, which is assembled at
build time from the `-PrepoSlug=owner/name` Gradle property.

## Building locally

Requirements: Android SDK (API 34 platform + build-tools) and Gradle 8.9.
No Gradle wrapper is checked in; CI downloads Gradle itself.

```sh
cd packaging/android/apk
gradle -PrepoSlug=owner/name assembleRelease
```

Output: `app/build/outputs/apk/release/app-release.apk`.

Without `-PrepoSlug` the URL falls back to `OWNER/REPO_PLACEHOLDER` (not a real
repository — only useful for a smoke build).

## Signing

Release signing is read from Gradle properties (or the matching
`ORG_GRADLE_PROJECT_*` environment variables):

| Property | Meaning |
| --- | --- |
| `RP_KEYSTORE` | Absolute path to the keystore file |
| `RP_KEYSTORE_PASSWORD` | Keystore password |
| `RP_KEY_ALIAS` | Key alias |
| `RP_KEY_PASSWORD` | Key password |

When `RP_KEYSTORE` is set, the release APK is signed with that keystore.
Otherwise the release build falls back to the debug keystore, so
`gradle assembleRelease` always produces a signed
`app/build/outputs/apk/release/app-release.apk`.

## CI

CI builds this project via `.github/workflows/release.yml`, which downloads
Gradle 8.9, passes `-PrepoSlug=...` and the signing properties, and uploads the
resulting APK as the release asset.
