package com.meowcoin.catstack.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.meowcoin.catstack.data.RigEntry

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RigListScreen(
    state: UiState,
    onOpenSettings: () -> Unit,
    onRefresh: () -> Unit,
    onRigClick: (RigEntry) -> Unit,
) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("CatStack") },
                actions = {
                    IconButton(onClick = onRefresh) {
                        Icon(Icons.Default.Refresh, contentDescription = "Refresh lists")
                    }
                    IconButton(onClick = onOpenSettings) {
                        Icon(Icons.Default.Settings, contentDescription = "Settings")
                    }
                },
            )
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            StatusBanner(state.statusMessage, state.isError)
            if (state.rigs.isEmpty()) {
                EmptyState(state)
            } else {
                LazyColumn(
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = PaddingValues(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    items(state.rigs, key = { it.name }) { rig ->
                        RigCard(rig) { onRigClick(rig) }
                    }
                }
            }
        }
    }
}

@Composable
private fun StatusBanner(message: String, isError: Boolean) {
    val bg = if (isError) MaterialTheme.colorScheme.errorContainer
        else MaterialTheme.colorScheme.surfaceVariant
    Box(
        Modifier
            .fillMaxWidth()
            .background(bg)
            .padding(horizontal = 16.dp, vertical = 6.dp),
    ) {
        Text(message, style = MaterialTheme.typography.labelMedium)
    }
}

@Composable
private fun EmptyState(state: UiState) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Text(
            if (!state.config.isConfigured) "Open settings to enter host + token"
            else "Waiting for stats…",
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun RigCard(rig: RigEntry, onClick: () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick),
        elevation = CardDefaults.cardElevation(2.dp),
    ) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                StatusDot(rig)
                Text(
                    rig.name,
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.padding(start = 8.dp),
                )
                Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.CenterEnd) {
                    Text(
                        formatHashrate(rig.hashrate, rig.algo),
                        style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Medium,
                    )
                }
            }
            Row(Modifier.padding(top = 4.dp)) {
                MetaItem("FS", rig.flight_sheet ?: "—")
                MetaItem("OC", rig.oc_profile ?: "—")
                MetaItem("Pwr", "%.0fW".format(rig.totalPowerWatts))
                rig.maxGpuTempC?.let { MetaItem("Tmax", "%.0f°C".format(it)) }
            }
        }
    }
}

@Composable
private fun StatusDot(rig: RigEntry) {
    val color = when {
        !rig.online -> Color(0xFFB00020)
        rig.isMining -> Color(0xFF2E7D32)
        else -> Color(0xFFFFA000)
    }
    Box(
        Modifier
            .size(10.dp)
            .clip(CircleShape)
            .background(color),
    )
}

@Composable
private fun MetaItem(label: String, value: String) {
    Column(Modifier.padding(end = 16.dp)) {
        Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, style = MaterialTheme.typography.bodySmall)
    }
}

private fun formatHashrate(hr: Double, algo: String?): String {
    if (hr <= 0.0) return algo?.let { "idle · $it" } ?: "idle"
    val (n, unit) = when {
        hr >= 1e9 -> hr / 1e9 to "GH/s"
        hr >= 1e6 -> hr / 1e6 to "MH/s"
        hr >= 1e3 -> hr / 1e3 to "kH/s"
        else -> hr to "H/s"
    }
    val pretty = if (n >= 100) "%.0f".format(n) else "%.2f".format(n)
    return algo?.let { "$pretty $unit · $it" } ?: "$pretty $unit"
}
