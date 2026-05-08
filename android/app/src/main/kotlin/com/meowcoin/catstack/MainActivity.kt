package com.meowcoin.catstack

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Scaffold
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.meowcoin.catstack.data.RigEntry
import com.meowcoin.catstack.ui.CatStackViewModel
import com.meowcoin.catstack.ui.RigActionSheet
import com.meowcoin.catstack.ui.RigListScreen
import com.meowcoin.catstack.ui.SettingsScreen

class MainActivity : ComponentActivity() {

    private val vm: CatStackViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
                Surface(modifier = Modifier, color = MaterialTheme.colorScheme.background) {
                    AppRoot(vm)
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AppRoot(vm: CatStackViewModel) {
    val state by vm.state.collectAsStateWithLifecycle()
    var showSettings by remember { mutableStateOf(false) }
    var sheetRig by remember { mutableStateOf<RigEntry?>(null) }
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    val snackbar = remember { SnackbarHostState() }

    LaunchedEffect(state.config.isConfigured) {
        if (!state.config.isConfigured) showSettings = true
    }

    LaunchedEffect(state.snackbar) {
        state.snackbar?.let {
            snackbar.showSnackbar(it)
            vm.snackShown()
        }
    }

    if (showSettings) {
        SettingsScreen(
            initial = state.config,
            onSave = vm::saveSettings,
            onBack = { showSettings = false },
        )
        return
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        Box(Modifier.padding(padding)) {
            RigListScreen(
                state = state,
                onOpenSettings = { showSettings = true },
                onRefresh = { vm.refreshLists() },
                onRigClick = { sheetRig = it },
            )
        }
    }

    val current = sheetRig
    if (current != null) {
        // Re-bind the sheet rig to the freshest entry so its mining state
        // updates while the sheet is open.
        val live = state.rigs.firstOrNull { it.name == current.name } ?: current
        RigActionSheet(
            rig = live,
            state = state,
            sheetState = sheetState,
            onDismiss = { sheetRig = null },
            onStart = { vm.startMiner(live.name) },
            onStop = { vm.stopMiner(live.name) },
            onRestart = { vm.restartMiner(live.name) },
            onApplyFlightSheet = { fs -> vm.applyFlightSheet(live.name, fs.name) },
            onApplyOcProfile = { oc -> vm.applyOcProfile(live.name, oc.name) },
        )
    }
}
