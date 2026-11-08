package mcbot;

import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.player.AsyncPlayerPreLoginEvent;
import org.bukkit.event.player.PlayerLoginEvent;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * Writes one log line for every connection the whitelist rejects, so the Discord bot can read
 * the player's name and UUID from latest.log:
 *
 *   [McbotWhitelist] REJECT <name> <uuid>
 *
 * The plugin only observes (MONITOR priority); it never changes who may join.
 */
public final class McbotWhitelist extends JavaPlugin implements Listener {
    @Override
    public void onEnable() {
        getServer().getPluginManager().registerEvents(this, this);
    }

    // Depending on the server version the whitelist is checked in one event or the other.
    @EventHandler(priority = EventPriority.MONITOR)
    public void onPreLogin(AsyncPlayerPreLoginEvent e) {
        if (e.getLoginResult() == AsyncPlayerPreLoginEvent.Result.KICK_WHITELIST) {
            report(e.getName(), e.getUniqueId().toString());
        }
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onLogin(PlayerLoginEvent e) {
        if (e.getResult() == PlayerLoginEvent.Result.KICK_WHITELIST) {
            report(e.getPlayer().getName(), e.getPlayer().getUniqueId().toString());
        }
    }

    private void report(String name, String uuid) {
        getLogger().info("REJECT " + name + " " + uuid);
    }
}
