package com.ani.callassistant.data

import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNamingStrategy
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import retrofit2.HttpException
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.util.concurrent.TimeUnit

@OptIn(ExperimentalSerializationApi::class)
val ApiJson = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    coerceInputValues = true
    namingStrategy = JsonNamingStrategy.SnakeCase
}

/**
 * Talks to the PC at home. At home the local address answers; away, the WireGuard
 * address does. The app tries both and keeps whichever answers GET /api/status.
 */
class ApiClient(private val prefs: Prefs) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(70, TimeUnit.SECONDS)   // events long-poll holds up to 60 s
        .addInterceptor { chain ->
            chain.proceed(chain.request().newBuilder()
                .header("Authorization", "Bearer ${prefs.token}")
                .build())
        }
        .build()

    private val probeHttp = http.newBuilder()
        .connectTimeout(2, TimeUnit.SECONDS)
        .readTimeout(3, TimeUnit.SECONDS)
        .build()

    @Volatile var baseUrl: String? = null
        private set
    private var api: CallAssistantApi? = null

    val authHeader: Map<String, String> get() = mapOf("Authorization" to "Bearer ${prefs.token}")

    /** Picks a reachable address. Returns null when the PC cannot be reached. */
    fun connect(): CallAssistantApi? {
        val candidates = listOf(prefs.localUrl, prefs.wireguardUrl).filter { it.isNotEmpty() }
        for (url in candidates) {
            if (reachable(url)) {
                if (url != baseUrl || api == null) {
                    baseUrl = url
                    api = Retrofit.Builder()
                        .baseUrl("$url/")
                        .client(http)
                        .addConverterFactory(ApiJson.asConverterFactory("application/json".toMediaType()))
                        .build()
                        .create(CallAssistantApi::class.java)
                }
                return api
            }
        }
        baseUrl = null
        api = null
        return null
    }

    /** The last working API, or a fresh connection attempt. */
    fun current(): CallAssistantApi? = api ?: connect()

    fun forget() {
        api = null
        baseUrl = null
    }

    fun absolute(path: String): String? = baseUrl?.let { it + path }

    private fun reachable(url: String): Boolean = try {
        probeHttp.newCall(Request.Builder().url("$url/api/status").build()).execute().use { it.isSuccessful }
    } catch (e: Exception) {
        false
    }
}

/** The server's {"error": ..., "detail": ...} message, or the exception text. */
fun Throwable.userMessage(): String {
    if (this is HttpException) {
        val body = response()?.errorBody()?.string()
        if (!body.isNullOrBlank()) {
            return try {
                ApiJson.parseToJsonElement(body).jsonObject["detail"]?.jsonPrimitive?.content ?: body
            } catch (e: Exception) {
                body
            }
        }
        return "HTTP ${code()}"
    }
    return message ?: javaClass.simpleName
}
