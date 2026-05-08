package com.meowcoin.catstack.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.SheetState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.meowcoin.catstack.data.FlightSheet
import com.meowcoin.catstack.data.OcProfile
import com.meowcoin.catstack.data.RigEntry

private enum class SheetView { Actions, FlightSheets, OcProfiles }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RigActionSheet(
    rig: RigEntry,
    state: UiState,
    sheetState: SheetState,
    onDismiss: () -> Unit,
    onStart: () -> Unit,
    onStop: () -> Unit,
    onRestart: () -> Unit,
    onApplyFlightSheet: (FlightSheet) -> Unit,
    onApplyOcProfile: (OcProfile) -> Unit,
) {
    var view by remember { mutableStateOf(SheetView.Actions) }

    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = sheetState) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 16.dp)) {
            Header(rig)
            HorizontalDivider(Modifier.padding(vertical = 12.dp))
            when (view) {
                SheetView.Actions -> ActionsView(
                    rig = rig,
                    onStart = { onStart(); onDismiss() },
                    onStop = { onStop(); onDismiss() },
                    onRestart = { onRestart(); onDismiss() },
                    onPickFlightSheet = { view = SheetView.FlightSheets },
                    onPickOcProfile = { view = SheetView.OcProfiles },
                )
                SheetView.FlightSheets -> PickerView(
                    title = "Apply flight sheet",
                    items = state.flightSheets,
                    label = { fs -> "${fs.name}  ·  ${fs.coin ?: "?"} / ${fs.algo ?: "?"}" },
                    onPick = { fs -> onApplyFlightSheet(fs); onDismiss() },
                    onBack = { view = SheetView.Actions },
                )
                SheetView.OcProfiles -> PickerView(
                    title = "Apply OC profile",
                    items = state.ocProfiles,
                    label = { oc ->
                        val parts = listOfNotNull(
                            oc.core_offset?.let { "core $it" },
                            oc.mem_offset?.let { "mem $it" },
                            oc.power_limit?.let { "${it}W" },
                        ).joinToString(" · ")
                        if (parts.isBlank()) oc.name else "${oc.name}  ·  $parts"
                    },
                    onPick = { oc -> onApplyOcProfile(oc); onDismiss() },
                    onBack = { view = SheetView.Actions },
                )
            }
        }
    }
}

@Composable
private fun Header(rig: RigEntry) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            rig.name,
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.SemiBold,
        )
        Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.CenterEnd) {
            Text(
                if (rig.online) (if (rig.isMining) "mining" else "online") else "offline",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
    rig.host?.let {
        Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun ActionsView(
    rig: RigEntry,
    onStart: () -> Unit,
    onStop: () -> Unit,
    onRestart: () -> Unit,
    onPickFlightSheet: () -> Unit,
    onPickOcProfile: () -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (rig.isMining) {
                Button(
                    onClick = onStop,
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.error,
                    ),
                ) { Text("Stop") }
                OutlinedButton(onClick = onRestart, modifier = Modifier.weight(1f)) { Text("Restart") }
            } else {
                Button(onClick = onStart, modifier = Modifier.weight(1f)) { Text("Start") }
                OutlinedButton(onClick = onRestart, modifier = Modifier.weight(1f)) { Text("Restart") }
            }
        }
        OutlinedButton(onClick = onPickFlightSheet, modifier = Modifier.fillMaxWidth()) {
            Text("Apply flight sheet" + (rig.flight_sheet?.let { "  ·  current: $it" } ?: ""))
        }
        OutlinedButton(onClick = onPickOcProfile, modifier = Modifier.fillMaxWidth()) {
            Text("Apply OC profile" + (rig.oc_profile?.let { "  ·  current: $it" } ?: ""))
        }
    }
}

@Composable
private fun <T> PickerView(
    title: String,
    items: List<T>,
    label: (T) -> String,
    onPick: (T) -> Unit,
    onBack: () -> Unit,
) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        TextButton(onClick = onBack) { Text("Back") }
        Text(title, style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(start = 4.dp))
    }
    if (items.isEmpty()) {
        Text(
            "Nothing to apply yet — create one in the desktop UI first.",
            modifier = Modifier.padding(vertical = 12.dp),
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    } else {
        LazyColumn(
            modifier = Modifier.heightIn(max = 360.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            items(items) { item ->
                OutlinedButton(
                    onClick = { onPick(item) },
                    modifier = Modifier.fillMaxWidth(),
                ) { Text(label(item)) }
            }
        }
    }
}
