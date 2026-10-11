package com.ani.callassistant

import android.app.Application
import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import com.ani.callassistant.data.ApiClient
import com.ani.callassistant.data.Prefs
import com.ani.callassistant.data.Repository
import com.ani.callassistant.data.db.CallDatabase
import com.ani.callassistant.service.Notifications
import java.io.File

class CallAssistantApp : Application() {
    lateinit var prefs: Prefs
        private set
    lateinit var client: ApiClient
        private set
    lateinit var repository: Repository
        private set

    /** Recordings are streamed, then kept here so a second play needs no connection. */
    @get:androidx.annotation.OptIn(UnstableApi::class)
    val audioCache: SimpleCache by lazy {
        SimpleCache(File(cacheDir, "audio"), LeastRecentlyUsedCacheEvictor(200L * 1024 * 1024),
                    StandaloneDatabaseProvider(this))
    }

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        client = ApiClient(prefs)
        repository = Repository(client, CallDatabase.build(this).calls())
        Notifications.createChannels(this)
    }
}
