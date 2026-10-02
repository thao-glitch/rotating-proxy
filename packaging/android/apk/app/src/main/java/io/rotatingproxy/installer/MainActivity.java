package io.rotatingproxy.installer;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

public class MainActivity extends Activity {

    private static final String TERMUX_FDROID_URL =
            "https://f-droid.org/packages/com.termux/";
    private static final String TERMUX_GITHUB_URL =
            "https://github.com/termux/termux-app/releases/latest";

    private LinearLayout root;
    private TextView statusView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(24);
        root.setPadding(pad, pad, pad, pad);

        TextView title = new TextView(this);
        title.setText("Rotating Proxy Installer");
        title.setTextSize(24f);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        root.addView(title);

        statusView = new TextView(this);
        statusView.setTextSize(16f);
        statusView.setGravity(Gravity.START);
        statusView.setPadding(0, dp(16), 0, dp(16));
        LinearLayout.LayoutParams statusParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        root.addView(statusView, statusParams);

        setContentView(root);

        try {
            if (isTermuxInstalled()) {
                attemptInstall();
            } else {
                showTermuxRequired();
            }
        } catch (Exception e) {
            statusView.setText("Unexpected error while starting: " + e);
        }
    }

    private boolean isTermuxInstalled() {
        try {
            getPackageManager().getPackageInfo("com.termux", 0);
            return true;
        } catch (PackageManager.NameNotFoundException e) {
            return false;
        }
    }

    private void showTermuxRequired() {
        statusView.setText("Rotating Proxy runs on Android inside Termux, "
                + "a terminal for Android. Install Termux, then open this app again.");

        Button install = new Button(this);
        install.setText("Install Termux (F-Droid)");
        install.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openUrl(TERMUX_FDROID_URL);
            }
        });
        root.addView(install);

        TextView github = new TextView(this);
        github.setText("Or get Termux from GitHub releases");
        github.setTextSize(14f);
        github.setTextColor(Color.GRAY);
        github.setPadding(0, dp(8), 0, 0);
        github.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openUrl(TERMUX_GITHUB_URL);
            }
        });
        root.addView(github);
    }

    private void attemptInstall() {
        Intent i = new Intent("com.termux.RUN_COMMAND");
        i.setComponent(new ComponentName("com.termux", "com.termux.app.RunCommandService"));
        i.putExtra("com.termux.RUN_COMMAND_PATH", "/data/data/com.termux/files/usr/bin/bash");
        i.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", new String[] { "-lc",
                "curl -fsSL '" + BuildConfig.RELEASE_SCRIPT_URL + "' | bash" });
        i.putExtra("com.termux.RUN_COMMAND_WORKDIR", "/data/data/com.termux/files/home");
        i.putExtra("com.termux.RUN_COMMAND_BACKGROUND", false);
        i.putExtra("com.termux.RUN_COMMAND_SESSION_ACTION", "0");
        try {
            startService(i);
            statusView.setText("Installation started — watch the Termux window. "
                    + "If nothing appears in Termux: (1) open Termux → Settings and "
                    + "enable \"Allow external apps\", (2) Android Settings → Apps → "
                    + "Rotating Proxy Installer → Permissions → Additional permissions "
                    + "→ allow \"Run commands in Termux environment\", then reopen "
                    + "this app.");
            addOpenSettingsButton();
        } catch (SecurityException e) {
            showNotReady(e);
        } catch (IllegalStateException e) {
            showNotReady(e);
        } catch (Exception e) {
            showNotReady(e);
        }
    }

    private void showNotReady(Exception e) {
        statusView.setText("Termux is installed but not ready yet (" + e + ").\n\n"
                + "Both manual steps are required before this app can run commands "
                + "in Termux:\n\n"
                + "(1) Open Termux → Settings and enable \"Allow external apps\" "
                + "(this sets allow-external-apps = true in "
                + "~/.termux/termux.properties).\n\n"
                + "(2) Android Settings → Apps → Rotating Proxy Installer → "
                + "Permissions → Additional permissions → allow \"Run commands in "
                + "Termux environment\".\n\n"
                + "Then reopen this app.");
        addOpenSettingsButton();
    }

    private void addOpenSettingsButton() {
        Button settings = new Button(this);
        settings.setText("Open app settings");
        settings.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openAppSettings();
            }
        });
        root.addView(settings);
    }

    private void openAppSettings() {
        try {
            startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:" + getPackageName())));
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, "Could not open app settings.",
                    Toast.LENGTH_LONG).show();
        }
    }

    private void openUrl(String url) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, "No browser found to open: " + url,
                    Toast.LENGTH_LONG).show();
        }
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
