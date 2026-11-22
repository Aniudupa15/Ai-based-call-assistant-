package com.ani.callassistant.ui

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.ContactsContract.CommonDataKinds.Phone
import androidx.activity.result.contract.ActivityResultContract
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ContactPhone
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SmallFloatingActionButton
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.dp
import com.ani.callassistant.CallAssistantApp
import com.ani.callassistant.data.Contact
import com.ani.callassistant.data.ContactIn
import com.ani.callassistant.data.Group
import com.ani.callassistant.data.Rule
import com.ani.callassistant.data.RuleIn
import com.ani.callassistant.data.userMessage
import kotlinx.coroutines.launch

private val LANGUAGE_OPTIONS = listOf(null to "Detect", "en" to "English", "hi" to "Hindi", "kn" to "Kannada")
private val ACTION_OPTIONS = listOf("answer" to "Answer", "reject" to "Reject", "ignore" to "Let it ring")
// "" clears the floor; null would be left out of the request and change nothing.
private val FLOOR_OPTIONS = listOf<Pair<String?, String>>("" to "None") + SEVERITIES.map { it to severityLabel(it) }

private object PickPhoneNumber : ActivityResultContract<Unit, Uri?>() {
    override fun createIntent(context: Context, input: Unit) = Intent(Intent.ACTION_PICK, Phone.CONTENT_URI)
    override fun parseResult(resultCode: Int, intent: Intent?): Uri? =
        if (resultCode == Activity.RESULT_OK) intent?.data else null
}

private data class ContactDraft(val id: Int?, val name: String, val number: String, val group: String, val language: String?, val notes: String)

@Composable
fun ContactsScreen() {
    val repo = (LocalContext.current.applicationContext as CallAssistantApp).repository
    val scope = rememberCoroutineScope()
    var tab by remember { mutableIntStateOf(0) }
    var contacts by remember { mutableStateOf<List<Contact>>(emptyList()) }
    var groups by remember { mutableStateOf<List<Group>>(emptyList()) }
    var rules by remember { mutableStateOf<List<Rule>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var draft by remember { mutableStateOf<ContactDraft?>(null) }
    var reload by remember { mutableIntStateOf(0) }

    LaunchedEffect(reload) {
        try {
            contacts = repo.contacts()
            groups = repo.groups()
            rules = repo.rules()
            error = null
        } catch (e: Exception) {
            error = e.userMessage()
        }
    }

    // Picking a number from the phone's contacts grants access to that one entry,
    // so no contacts permission is needed.
    val context = LocalContext.current
    val picker = rememberLauncherForActivityResult(PickPhoneNumber) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        val picked = context.contentResolver.query(uri,
            arrayOf(Phone.DISPLAY_NAME, Phone.NUMBER), null, null, null)?.use { c ->
            if (c.moveToFirst()) c.getString(0) to c.getString(1) else null
        }
        draft = ContactDraft(null, picked?.first ?: "", picked?.second ?: "", "known", null, "")
    }

    Box(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize()) {
            TabRow(selectedTabIndex = tab) {
                Tab(selected = tab == 0, onClick = { tab = 0 }, text = { Text("Contacts") })
                Tab(selected = tab == 1, onClick = { tab = 1 }, text = { Text("Rules") })
            }
            error?.let { MessageBanner(it) }
            if (tab == 0) {
                LazyColumn(Modifier.fillMaxSize()) {
                    items(contacts, key = { it.id }) { c ->
                        Column(Modifier.fillMaxWidth().clickable {
                            draft = ContactDraft(c.id, c.name, c.number, c.groupName ?: "known", c.languageHint, c.notes)
                        }.padding(horizontal = 16.dp, vertical = 10.dp)) {
                            Text(c.name, style = MaterialTheme.typography.bodyLarge)
                            Text("${c.number} · ${c.groupName ?: "known"}" +
                                 (c.languageHint?.let { " · ${LANGUAGES[it]}" } ?: ""),
                                 style = MaterialTheme.typography.bodySmall,
                                 color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        HorizontalDivider()
                    }
                }
            } else {
                RulesTab(groups, rules,
                    onGroupRule = { g, r -> scope.launch { runCatching { repo.setGroupRule(g, r) }.onFailure { error = it.userMessage() }; reload++ } },
                    onSaveRule = { id, body -> scope.launch { runCatching { repo.updateRule(id, body) }.onFailure { error = it.userMessage() }; reload++ } })
            }
        }
        if (tab == 0) {
            Column(Modifier.align(Alignment.BottomEnd).padding(16.dp), horizontalAlignment = Alignment.End,
                   verticalArrangement = Arrangement.spacedBy(12.dp)) {
                SmallFloatingActionButton(onClick = { picker.launch(Unit) }) {
                    Icon(Icons.Filled.ContactPhone, "Import from phone contacts")
                }
                ExtendedFloatingActionButton(onClick = { draft = ContactDraft(null, "", "", "known", null, "") },
                    icon = { Icon(Icons.Filled.Add, null) }, text = { Text("Add") })
            }
        }
    }

    draft?.let { d ->
        ContactDialog(d, groups,
            onDismiss = { draft = null },
            onSave = { saved ->
                scope.launch {
                    try {
                        repo.saveContact(saved.id, ContactIn(saved.name, saved.number, saved.group, saved.language, saved.notes))
                        draft = null
                        reload++
                    } catch (e: Exception) {
                        error = e.userMessage()
                    }
                }
            },
            onDelete = if (d.id == null) null else {
                {
                    scope.launch {
                        runCatching { repo.deleteContact(d.id) }.onFailure { error = it.userMessage() }
                        draft = null
                        reload++
                    }
                }
            })
    }
}

@Composable
private fun ContactDialog(
    initial: ContactDraft,
    groups: List<Group>,
    onDismiss: () -> Unit,
    onSave: (ContactDraft) -> Unit,
    onDelete: (() -> Unit)?,
) {
    var d by remember { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(if (initial.id == null) "Add contact" else "Edit contact") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(d.name, { d = d.copy(name = it) }, label = { Text("Name") }, singleLine = true)
                OutlinedTextField(d.number, { d = d.copy(number = it) }, label = { Text("Number") }, singleLine = true,
                                  keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone))
                OptionPicker("Group", d.group, groups.map { it.name to it.name.replaceFirstChar { c -> c.uppercase() } },
                             onSelect = { d = d.copy(group = it ?: "known") })
                OptionPicker("Language", d.language, LANGUAGE_OPTIONS, onSelect = { d = d.copy(language = it) })
                OutlinedTextField(d.notes, { d = d.copy(notes = it) }, label = { Text("Notes") })
            }
        },
        confirmButton = {
            TextButton(onClick = { onSave(d) }, enabled = d.name.isNotBlank() && d.number.isNotBlank()) { Text("Save") }
        },
        dismissButton = {
            Row {
                onDelete?.let { TextButton(onClick = it) { Text("Delete") } }
                TextButton(onClick = onDismiss) { Text("Cancel") }
            }
        }
    )
}

@Composable
private fun RulesTab(
    groups: List<Group>,
    rules: List<Rule>,
    onGroupRule: (Int, Int) -> Unit,
    onSaveRule: (Int, RuleIn) -> Unit,
) {
    LazyColumn(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item { Text("Each group follows one rule.", style = MaterialTheme.typography.titleSmall) }
        items(groups, key = { "g${it.id}" }) { g ->
            OptionPicker(g.name.replaceFirstChar { it.uppercase() }, g.ruleId?.toString(),
                         rules.map { it.id.toString() to it.name },
                         onSelect = { it?.toIntOrNull()?.let { r -> onGroupRule(g.id, r) } })
        }
        item { Text("Rules", style = MaterialTheme.typography.titleSmall) }
        items(rules, key = { "r${it.id}" }) { r -> RuleCard(r, onSaveRule) }
    }
}

@Composable
private fun RuleCard(rule: Rule, onSave: (Int, RuleIn) -> Unit) {
    var action by remember(rule) { mutableStateOf(rule.action) }
    var prompt by remember(rule) { mutableStateOf(rule.extraPrompt) }
    var maxSeconds by remember(rule) { mutableStateOf(rule.maxSeconds?.toString() ?: "") }
    var floor by remember(rule) { mutableStateOf(rule.severityFloor ?: "") }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(rule.name.replaceFirstChar { it.uppercase() }, style = MaterialTheme.typography.titleMedium)
            OptionPicker("Action", action, ACTION_OPTIONS, onSelect = { action = it ?: "answer" })
            OutlinedTextField(prompt, { prompt = it }, label = { Text("What the assistant may say") },
                              modifier = Modifier.fillMaxWidth())
            OutlinedTextField(maxSeconds, { maxSeconds = it.filter(Char::isDigit) }, label = { Text("Time limit, seconds") },
                              singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number))
            OptionPicker("Lowest severity", floor, FLOOR_OPTIONS, onSelect = { floor = it ?: "" })
            TextButton(onClick = {
                onSave(rule.id, RuleIn(action = action, extraPrompt = prompt,
                                       maxSeconds = maxSeconds.toIntOrNull(), severityFloor = floor))
            }) { Text("Save rule") }
        }
    }
}
