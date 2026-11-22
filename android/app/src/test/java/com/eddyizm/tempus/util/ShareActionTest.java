package com.eddyizm.tempus.util;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import com.eddyizm.tempus.subsonic.models.Share;

import org.junit.Test;

public class ShareActionTest {
    @Test
    public void unavailableSharingHidesAction() {
        assertFalse(ShareAction.isAvailable(false));
        assertTrue(ShareAction.isAvailable(true));
    }

    @Test
    public void successfulShareCopiesUrlAndReportsSuccess() {
        Share share = new Share();
        share.setUrl("https://example.test/share/1");
        String[] copied = {null};
        String[] result = {null};

        ShareAction.complete(share, url -> {
            copied[0] = url;
            return true;
        }, new ShareAction.Result() {
            @Override
            public void onSuccess() {
                result[0] = "success";
            }

            @Override
            public void onError() {
                result[0] = "error";
            }
        });

        assertEquals("https://example.test/share/1", copied[0]);
        assertEquals("success", result[0]);
    }

    @Test
    public void failedShareReportsErrorWithoutCopying() {
        ShareAction.Result result = new ShareAction.Result() {
            @Override
            public void onSuccess() {
                throw new AssertionError("Unexpected success");
            }

            @Override
            public void onError() {
                assertTrue(true);
            }
        };
        ShareAction.complete(null, url -> {
            throw new AssertionError("Should not copy a missing share");
        }, result);
    }

    @Test
    public void emptyUrlReportsErrorWithoutCopying() {
        Share share = new Share();
        share.setUrl(" ");
        boolean[] copied = {false};
        String[] result = {null};

        ShareAction.complete(share, url -> {
            copied[0] = true;
            return true;
        }, new ShareAction.Result() {
            @Override
            public void onSuccess() {
                result[0] = "success";
            }

            @Override
            public void onError() {
                result[0] = "error";
            }
        });

        assertFalse(copied[0]);
        assertEquals("error", result[0]);
    }
}
