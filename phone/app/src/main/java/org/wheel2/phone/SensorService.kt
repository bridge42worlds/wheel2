package org.wheel2.phone

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import kotlin.math.roundToInt

/**
 * Датчики, связь и программа — одни на приложение и живут в службе: экран их только показывает.
 * Так замеры идут и с погашенным экраном, и когда приложение свёрнуто.
 */
class Rig private constructor(ctx: Context) {
    val hub = SensorHub(ctx)
    val up = Uplink(ctx, hub)
    val exp = Experiment(up, hub)

    companion object {
        @Volatile private var one: Rig? = null
        fun get(ctx: Context): Rig = one ?: synchronized(this) { one ?: Rig(ctx.applicationContext).also { one = it } }

        /** Мерить ли: «■ стоп» на экране снимает, «▶ мерить» ставит; по нему же служба поднимается после перезагрузки. */
        fun wanted(ctx: Context) = ctx.getSharedPreferences("rig", Context.MODE_PRIVATE).getBoolean("run", true)
        fun setWanted(ctx: Context, on: Boolean) = ctx.getSharedPreferences("rig", Context.MODE_PRIVATE).edit().putBoolean("run", on).apply()
    }
}

/**
 * Служба переднего плана (тип health — датчики с частотой выше 200 Гц): значок в шторке, процессор не засыпает
 * (PARTIAL_WAKE_LOCK), Wi-Fi не уходит в экономный режим (WifiLock). Убитая системой — поднимается снова
 * (START_STICKY); после перезагрузки телефона — BootReceiver. Без «работы без ограничений батареи» Android может
 * всё равно придержать сеть в глубоком сне неподвижного телефона — экран просит это разрешение.
 */
class SensorService : Service() {
    private var wake: PowerManager.WakeLock? = null
    private var wifi: WifiManager.WifiLock? = null
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var rig: Rig

    override fun onBind(i: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        rig = Rig.get(this)
        try {
            if (Build.VERSION.SDK_INT >= 34) startForeground(NOTE_ID, note(), ServiceInfo.FOREGROUND_SERVICE_TYPE_HEALTH)
            else startForeground(NOTE_ID, note())
        } catch (e: Exception) {                      // система не дала выйти на передний план (поднята из фона) — не падать
            running = false
            stopSelf()
            return
        }
        wake = (getSystemService(POWER_SERVICE) as PowerManager).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "wheel2:sensors")
            .apply { setReferenceCounted(false); acquire() }
        @Suppress("DEPRECATION")
        wifi = (applicationContext.getSystemService(WIFI_SERVICE) as WifiManager)
            .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "wheel2:uplink").apply { setReferenceCounted(false); acquire() }
        rig.hub.start()
        rig.up.start()
        rig.exp.start()
        running = true
        ui.postDelayed(refresh, 5000)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        ui.removeCallbacks(refresh)
        if (::rig.isInitialized) {
            rig.exp.stop()
            rig.hub.stop()
            rig.up.stop()
        }
        wifi?.let { if (it.isHeld) it.release() }
        wake?.let { if (it.isHeld) it.release() }
        running = false
        super.onDestroy()
    }

    // строка в шторке: связь и частота — раз в 5 с
    private val refresh = object : Runnable {
        override fun run() {
            (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).notify(NOTE_ID, note())
            ui.postDelayed(this, 5000)
        }
    }

    private fun note(): Notification {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (nm.getNotificationChannel(CHANNEL) == null)
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Замеры установки", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Датчики телефона меряют и отправляют на сервер «Колесо-2»"
                setShowBadge(false)
            })
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE)
        val text = if (!::rig.isInitialized) "запуск…" else {
            val up = rig.up
            val fresh = up.ok && SystemClock.elapsedRealtime() - up.lastOkMs < 5000
            (if (fresh) "✓ на связи " + (if (up.url == Uplink.USB_URL) "по USB" else "по Wi-Fi") else "✗ нет связи с сервером") +
                " · акселерометр ${rig.hub.accRate.hz.roundToInt()} Гц · поле ${rig.hub.magRate.hz.roundToInt()} Гц"
        }
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setContentTitle("Колесо-2: датчик меряет")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(open)
            .build()
    }

    companion object {
        const val NOTE_ID = 9
        const val CHANNEL = "rig"
        @Volatile var running = false

        fun start(ctx: Context) {
            try {
                ctx.startForegroundService(Intent(ctx, SensorService::class.java))
            } catch (_: Exception) {                  // из фона без разрешения — поднимется, когда откроют приложение
            }
        }

        fun stop(ctx: Context) = ctx.stopService(Intent(ctx, SensorService::class.java))
    }
}

/** После включения телефона — снова мерить (если перед этим не нажали «■ стоп»). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED && Rig.wanted(ctx)) SensorService.start(ctx)
    }
}
