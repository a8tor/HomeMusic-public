package com.eddyizm.tempus.util;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;

import com.eddyizm.tempus.subsonic.models.Share;

public final class ShareAction {
    public interface Clipboard {
        boolean copy(String url);
    }

    public interface Result {
        void onSuccess();
        void onError();
    }

    private ShareAction() {
    }

    public static boolean copyToClipboard(@NonNull Context context, @NonNull String url) {
        ClipboardManager clipboardManager = (ClipboardManager) context.getSystemService(Context.CLIPBOARD_SERVICE);
        if (clipboardManager == null) return false;
        clipboardManager.setPrimaryClip(ClipData.newPlainText(context.getString(com.eddyizm.tempus.R.string.app_name), url));
        return true;
    }

    public static boolean isAvailable(boolean sharingEnabled) {
        return sharingEnabled;
    }

    public static void complete(@Nullable Share share, Clipboard clipboard, Result result) {
        String url = share == null ? null : share.getUrl();
        if (url == null || url.trim().isEmpty()) {
            result.onError();
            return;
        }
        if (!clipboard.copy(url)) {
            result.onError();
            return;
        }
        result.onSuccess();
    }
}
