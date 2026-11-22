package com.ani.callassistant.data

import kotlinx.serialization.Serializable
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.PATCH
import retrofit2.http.POST
import retrofit2.http.PUT
import retrofit2.http.Path
import retrofit2.http.Query

// Bodies of the PC's REST API (server/app/api.py). JSON keys are snake_case;
// the Json instance in ApiClient maps them.

@Serializable
data class CallSummary(
    val id: String,
    val number: String = "",
    val contact: String? = null,
    val callerName: String? = null,
    val startedAt: Long = 0,
    val durationS: Int = 0,
    val handledBy: String = "bot",
    val status: String = "ready",
    val language: String? = null,
    val severity: String? = null,
    val severityModel: String? = null,
    val severityCorrected: String? = null,
    val severityWhy: String? = null,
    val reason: String? = null,
    val summary: String? = null,
    val actionNeeded: String? = null,
    val deadline: String? = null,
    val callbackNumber: String? = null,
    val flags: List<String> = emptyList(),
    val endReason: String? = null,
    val seen: Boolean = false,
    val audioUrl: String? = null,
    val botCommitments: List<String> = emptyList(),
    val turns: List<TranscriptTurn> = emptyList(),
) {
    val displayName: String get() = contact ?: callerName ?: number.ifEmpty { "Withheld number" }
}

@Serializable
data class TranscriptTurn(
    val seq: Int,
    val speaker: String,
    val startMs: Long = 0,
    val endMs: Long = 0,
    val language: String? = null,
    val text: String = "",
    val textEn: String? = null,
)

@Serializable
data class CallList(val calls: List<CallSummary>)

@Serializable
data class CallPatch(val seen: Boolean? = null, val severityCorrected: String? = null)

@Serializable
data class Contact(
    val id: Int = 0,
    val name: String,
    val number: String,
    val groupId: Int? = null,
    val groupName: String? = null,
    val languageHint: String? = null,
    val notes: String = "",
)

@Serializable
data class ContactIn(
    val name: String,
    val number: String,
    val group: String? = null,
    val languageHint: String? = null,
    val notes: String = "",
)

@Serializable
data class ContactList(val contacts: List<Contact>)

@Serializable
data class Group(val id: Int, val name: String, val ruleId: Int? = null, val ruleName: String? = null)

@Serializable
data class GroupList(val groups: List<Group>)

@Serializable
data class GroupPatch(val ruleId: Int)

@Serializable
data class Rule(
    val id: Int,
    val name: String,
    val action: String = "answer",
    val languageHint: String? = null,
    val extraPrompt: String = "",
    val maxSeconds: Int? = null,
    val severityFloor: String? = null,
)

@Serializable
data class RuleIn(
    val action: String? = null,
    val extraPrompt: String? = null,
    val maxSeconds: Int? = null,
    val severityFloor: String? = null,
)

@Serializable
data class RuleList(val rules: List<Rule>)

@Serializable
data class DeviceInfo(
    val deviceId: String? = null,
    val fwVersion: String? = null,
    val vmPending: Int? = null,
)

@Serializable
data class Health(
    val ts: Long = 0,
    val registered: Boolean = false,
    val rssi: Int = 99,
    val sdFreeMb: Int = 0,
    val onBattery: Boolean = false,
)

@Serializable
data class Status(
    val server: String = "",
    val time: Long = 0,
    val espConnected: Boolean = false,
    val espSince: Long? = null,
    val device: DeviceInfo? = null,
    val callLive: Boolean = false,
    val health: Health? = null,
    val modelsLoaded: List<String> = emptyList(),
    val pendingSms: Int = 0,
)

@Serializable
data class Event(
    val seq: Long,
    val ts: Long = 0,
    val kind: String,
    val callId: String? = null,
    val severity: String? = null,
    val summary: String? = null,
    val caller: String? = null,
)

@Serializable
data class EventList(val events: List<Event>, val lastSeq: Long)

@Serializable
data class OwnerSettings(
    val ownerName: String = "",
    val ownerNumber: String = "",
    val retentionDays: Int = 30,
)

@Serializable
data class TestSms(val text: String = "Test message from your call assistant.")

@Serializable
data class Ok(val ok: Boolean = true)

interface CallAssistantApi {
    @GET("api/status")
    suspend fun status(): Status

    @GET("api/events")
    suspend fun events(@Query("after") after: Long, @Query("timeout") timeout: Int = 55): EventList

    @GET("api/calls")
    suspend fun calls(
        @Query("severity") severity: String? = null,
        @Query("limit") limit: Int = 200,
    ): CallList

    @GET("api/calls/{id}")
    suspend fun call(@Path("id") id: String): CallSummary

    @PATCH("api/calls/{id}")
    suspend fun patchCall(@Path("id") id: String, @Body body: CallPatch): CallSummary

    @DELETE("api/calls/{id}")
    suspend fun deleteCall(@Path("id") id: String): Ok

    @GET("api/contacts")
    suspend fun contacts(): ContactList

    @POST("api/contacts")
    suspend fun addContact(@Body body: ContactIn): Contact

    @PUT("api/contacts/{id}")
    suspend fun updateContact(@Path("id") id: Int, @Body body: ContactIn): Contact

    @DELETE("api/contacts/{id}")
    suspend fun deleteContact(@Path("id") id: Int): Ok

    @GET("api/groups")
    suspend fun groups(): GroupList

    @PUT("api/groups/{id}")
    suspend fun setGroupRule(@Path("id") id: Int, @Body body: GroupPatch): Group

    @GET("api/rules")
    suspend fun rules(): RuleList

    @PUT("api/rules/{id}")
    suspend fun updateRule(@Path("id") id: Int, @Body body: RuleIn): Rule

    @GET("api/settings")
    suspend fun settings(): OwnerSettings

    @PUT("api/settings")
    suspend fun putSettings(@Body body: OwnerSettings): OwnerSettings

    @POST("api/test/sms")
    suspend fun testSms(@Body body: TestSms): Ok
}
