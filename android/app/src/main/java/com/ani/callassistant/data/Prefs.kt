package com.ani.callassistant.data

import android.content.Context

/** Connection settings, entered on first launch in Settings. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("call_assistant", Context.MODE_PRIVATE)

    /** The PC's address on the home network, e.g. http://192.168.1.50:8765 */
    var localUrl: String
        get() = sp.getString("local_url", "") ?: ""
        set(v) = sp.edit().putString("local_url", v.trim().trimEnd('/')).apply()

    /** The PC's address inside the WireGuard tunnel, e.g. http://10.8.0.1:8765 */
    var wireguardUrl: String
        get() = sp.getString("wireguard_url", "") ?: ""
        set(v) = sp.edit().putString("wireguard_url", v.trim().trimEnd('/')).apply()

    var token: String
        get() = sp.getString("token", "") ?: ""
        set(v) = sp.edit().putString("token", v.trim()).apply()

    /** Module number, for the call-forwarding codes. */
    var moduleNumber: String
        get() = sp.getString("module_number", "") ?: ""
        set(v) = sp.edit().putString("module_number", v.trim()).apply()

    var lastEventSeq: Long
        get() = sp.getLong("last_event_seq", 0)
        set(v) = sp.edit().putLong("last_event_seq", v).apply()

    val configured: Boolean get() = token.isNotEmpty() && (localUrl.isNotEmpty() || wireguardUrl.isNotEmpty())
}
