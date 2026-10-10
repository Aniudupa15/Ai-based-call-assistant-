package com.ani.callassistant.data

import com.ani.callassistant.data.db.CallDao
import com.ani.callassistant.data.db.CallEntity
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.withContext

class OfflineException : Exception("The PC is not reachable. Showing saved history.")

/** Calls come from the Room cache; refresh() and the record screen update it from the PC. */
class Repository(private val client: ApiClient, private val dao: CallDao) {

    val calls: Flow<List<CallSummary>> = dao.all().map { rows -> rows.map { decode(it.json) } }

    fun call(id: String): Flow<CallSummary?> = dao.byId(id).map { it?.let { row -> decode(row.json) } }

    private fun decode(json: String) = ApiJson.decodeFromString(CallSummary.serializer(), json)

    private fun encode(c: CallSummary) = ApiJson.encodeToString(CallSummary.serializer(), c)

    suspend fun api(): CallAssistantApi = withContext(Dispatchers.IO) {
        client.current() ?: client.connect() ?: throw OfflineException()
    }

    /** Runs a call, reconnecting once if the saved address stopped answering. */
    private suspend fun <T> withApi(block: suspend (CallAssistantApi) -> T): T = try {
        block(api())
    } catch (e: java.io.IOException) {
        client.forget()
        block(api())
    }

    suspend fun refresh() {
        val list = withApi { it.calls() }.calls
        val existing = list.associate { c -> c.id to dao.get(c.id) }
        dao.put(list.map { c ->
            val cached = existing[c.id]
            // Keep a cached full record unless the summary changed underneath it.
            if (cached != null && cached.hasDetail) {
                val old = decode(cached.json)
                if (old.status == c.status && old.severity == c.severity && old.seen == c.seen) {
                    return@map cached
                }
            }
            CallEntity(c.id, c.startedAt, encode(c), hasDetail = false)
        })
        dao.deleteAllExcept(list.map { it.id })
    }

    suspend fun loadDetail(id: String): CallSummary {
        val c = withApi { it.call(id) }
        dao.put(listOf(CallEntity(c.id, c.startedAt, encode(c), hasDetail = true)))
        return c
    }

    suspend fun markSeen(id: String) = save(withApi { it.patchCall(id, CallPatch(seen = true)) })

    suspend fun correctSeverity(id: String, severity: String) =
        save(withApi { it.patchCall(id, CallPatch(severityCorrected = severity)) })

    suspend fun delete(id: String) {
        withApi { it.deleteCall(id) }
        dao.delete(id)
    }

    private suspend fun save(c: CallSummary) {
        dao.put(listOf(CallEntity(c.id, c.startedAt, encode(c), hasDetail = c.turns.isNotEmpty())))
    }

    fun audioUrl(c: CallSummary): String? = c.audioUrl?.let { client.absolute(it) }

    val authHeader: Map<String, String> get() = client.authHeader

    // ---- contacts and rules ----------------------------------------------------
    suspend fun contacts() = withApi { it.contacts() }.contacts
    suspend fun groups() = withApi { it.groups() }.groups
    suspend fun rules() = withApi { it.rules() }.rules
    suspend fun saveContact(id: Int?, body: ContactIn) =
        withApi { if (id == null) it.addContact(body) else it.updateContact(id, body) }
    suspend fun deleteContact(id: Int) = withApi { it.deleteContact(id) }
    suspend fun updateRule(id: Int, body: RuleIn) = withApi { it.updateRule(id, body) }
    suspend fun setGroupRule(groupId: Int, ruleId: Int) = withApi { it.setGroupRule(groupId, GroupPatch(ruleId)) }

    // ---- status and settings ---------------------------------------------------
    suspend fun status() = withApi { it.status() }
    suspend fun settings() = withApi { it.settings() }
    suspend fun saveSettings(s: OwnerSettings) = withApi { it.putSettings(s) }
    suspend fun testSms() = withApi { it.testSms(TestSms()) }
}
