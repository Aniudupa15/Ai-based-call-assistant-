package com.ani.callassistant

import android.Manifest
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Contacts
import androidx.compose.material.icons.filled.MonitorHeart
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.ani.callassistant.service.EventPollService
import com.ani.callassistant.service.Notifications
import com.ani.callassistant.ui.CallRecordScreen
import com.ani.callassistant.ui.CallsScreen
import com.ani.callassistant.ui.ContactsScreen
import com.ani.callassistant.ui.SettingsScreen
import com.ani.callassistant.ui.StatusScreen
import com.ani.callassistant.ui.theme.CallAssistantTheme

private data class Tab(val route: String, val label: String, val icon: ImageVector)

private val TABS = listOf(
    Tab("calls", "Calls", Icons.AutoMirrored.Filled.List),
    Tab("contacts", "Contacts", Icons.Filled.Contacts),
    Tab("status", "Status", Icons.Filled.MonitorHeart),
    Tab("settings", "Settings", Icons.Filled.Settings),
)

class MainActivity : ComponentActivity() {
    /** A call ID from a tapped notification, opened once the UI is up. */
    private val pendingCall = mutableStateOf<String?>(null)

    private val askNotifications =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        pendingCall.value = intent?.getStringExtra(Notifications.EXTRA_CALL_ID)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        val app = application as CallAssistantApp
        if (app.prefs.configured) EventPollService.start(this)

        setContent {
            CallAssistantTheme {
                App(startRoute = if (app.prefs.configured) "calls" else "settings", pendingCall = pendingCall.value,
                    onPendingHandled = { pendingCall.value = null })
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        intent.getStringExtra(Notifications.EXTRA_CALL_ID)?.let { pendingCall.value = it }
    }
}

@Composable
private fun App(startRoute: String, pendingCall: String?, onPendingHandled: () -> Unit) {
    val nav = rememberNavController()
    val backStack by nav.currentBackStackEntryAsState()
    val route = backStack?.destination?.route

    LaunchedEffect(pendingCall) {
        if (pendingCall != null) {
            nav.navigate("call/$pendingCall")
            onPendingHandled()
        }
    }

    Scaffold(bottomBar = {
        if (route?.startsWith("call/") != true) {
            NavigationBar {
                TABS.forEach { tab ->
                    NavigationBarItem(
                        selected = route == tab.route,
                        onClick = { nav.switchTab(tab.route) },
                        icon = { Icon(tab.icon, null) },
                        label = { Text(tab.label) }
                    )
                }
            }
        }
    }) { padding ->
        NavHost(nav, startDestination = startRoute, modifier = Modifier.padding(padding)) {
            composable("calls") { CallsScreen(onOpen = { nav.navigate("call/$it") }) }
            composable("call/{id}") { entry ->
                CallRecordScreen(callId = entry.arguments?.getString("id") ?: "", onBack = { nav.popBackStack() })
            }
            composable("contacts") { ContactsScreen() }
            composable("status") { StatusScreen() }
            composable("settings") { SettingsScreen() }
        }
    }
}

private fun NavHostController.switchTab(route: String) {
    navigate(route) {
        popUpTo(graph.findStartDestination().id) { saveState = true }
        launchSingleTop = true
        restoreState = true
    }
}
