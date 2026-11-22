package com.eddyizm.tempus.util

import android.content.Context
import androidx.media3.common.MediaItem
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.drm.DrmSessionManagerProvider
import androidx.media3.exoplayer.source.MediaSource
import androidx.media3.exoplayer.upstream.LoadErrorHandlingPolicy
import org.junit.Assert.assertNotNull
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.JUnit4
import org.mockito.Mockito.mock
import org.mockito.MockedStatic
import org.mockito.Mockito.mockStatic

@UnstableApi
@RunWith(JUnit4::class)
class DynamicMediaSourceFactoryTest {
    @Test
    fun settersDoNotThrowAndFactoryCreatesSource() {
        val context = mock(Context::class.java)
        val parsedUri = mock(android.net.Uri::class.java)
        val uriStatic: MockedStatic<android.net.Uri> = mockStatic(android.net.Uri::class.java)
        uriStatic.`when`<android.net.Uri> { android.net.Uri.parse("https://example.test/track.m3u8") }
            .thenReturn(parsedUri)
        val preferences: MockedStatic<Preferences> = mockStatic(Preferences::class.java)
        preferences.`when`<Long> { Preferences.getStreamingCacheSize() }.thenReturn(0L)
        val dataSourceFactory = androidx.media3.datasource.DefaultDataSource.Factory(context)
        val downloadUtil: MockedStatic<DownloadUtil> = mockStatic(DownloadUtil::class.java)
        downloadUtil.`when`<androidx.media3.datasource.DataSource.Factory> {
            DownloadUtil.getUpstreamDataSourceFactory(context)
        }.thenReturn(dataSourceFactory)
        val httpDataSourceFactory = androidx.media3.datasource.DefaultHttpDataSource.Factory()
        downloadUtil.`when`<androidx.media3.datasource.DataSource.Factory> {
            DownloadUtil.getHttpDataSourceFactory()
        }.thenReturn(httpDataSourceFactory)
        val offsetExtension: MockedStatic<OpenSubsonicExtensionsUtil> = mockStatic(OpenSubsonicExtensionsUtil::class.java)
        offsetExtension.`when`<Boolean> { OpenSubsonicExtensionsUtil.isTranscodeOffsetExtensionAvailable() }
            .thenReturn(false)
        val factory = DynamicMediaSourceFactory(context)
        val drmProvider = mock(DrmSessionManagerProvider::class.java)
        val errorPolicy = mock(LoadErrorHandlingPolicy::class.java)

        try {
            assertNotNull(factory.setDrmSessionManagerProvider(drmProvider))
            assertNotNull(factory.setLoadErrorHandlingPolicy(errorPolicy))

            val source: MediaSource = factory.createMediaSource(
                MediaItem.Builder()
                    .setUri(parsedUri)
                    .setMimeType(androidx.media3.common.MimeTypes.APPLICATION_M3U8)
                    .build()
            )
            assertNotNull(source)
        } finally {
            offsetExtension.close()
            downloadUtil.close()
            preferences.close()
            uriStatic.close()
        }
    }
}
