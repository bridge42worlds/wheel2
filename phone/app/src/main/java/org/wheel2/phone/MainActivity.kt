package org.wheel2.phone

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Canvas
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowInsets
import android.view.WindowManager
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.TextView
import java.util.Locale
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.exp
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sqrt

/** Цвета Okabe — Ito (различимы при дальтонизме); смысл всегда дублирует значок и слово. */
object Ok {
    const val BG = 0xFF101418.toInt()
    const val CARD = 0xFF1A2027.toInt()
    const val INK = 0xFFE8EDF2.toInt()
    const val DIM = 0xFF9AA5B1.toInt()
    const val BLUE = 0xFF56B4E9.toInt()      // хорошо
    const val ORANGE = 0xFFE69F00.toInt()    // внимание
    const val VERM = 0xFFD55E00.toInt()      // плохо
    const val GREEN = 0xFF009E73.toInt()
    const val YELLOW = 0xFFF0E442.toInt()
    const val SEL = 0xFF2F4F6F.toInt()       // выбранная кнопка
    const val BTN = 0xFF252D37.toInt()       // кнопка
}

private val RU: Locale = Locale.forLanguageTag("ru")
private fun f(v: Double, d: Int) = String.format(RU, "%.${d}f", v)
private fun fs(v: Double, d: Int) = String.format(RU, "%+.${d}f", v).replace('-', '−')

/**
 * Телефон (или планшет) — детектор установки «Колесо-2»: акселерометр, магнитометр и фон (сеть 50 Гц,
 * радио). Вкладки: «датчики» — значения и колебания за 10 с; «Гаусс» — распределение по оси или модулю
 * за окно 1–60 с или с начала шага программы; «планшетка» — страница установки с сервера (датчики при
 * этом продолжают мерить). Сверху — связь и что сейчас делает установка. Меряет служба переднего плана
 * (SensorService): экран можно гасить, приложение сворачивать — значок в шторке, пока идут замеры.
 */
class MainActivity : Activity() {
    private lateinit var hub: SensorHub
    private lateinit var up: Uplink
    private lateinit var exp: Experiment
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var status: TextView
    private lateinit var expLine: TextView
    private lateinit var accHead: TextView
    private lateinit var accVal: TextView
    private lateinit var accSd: TextView
    private lateinit var accPlot: LanePlot
    private lateinit var magHead: TextView
    private lateinit var magVal: TextView
    private lateinit var magSd: TextView
    private lateinit var magPlot: LanePlot
    private lateinit var bg: TextView
    private lateinit var foot: TextView
    private var screen = 0
    private lateinit var screenBtn: Button
    private lateinit var runBtn: Button
    private lateinit var batBtn: Button

    // вкладки
    private val tabs = ArrayList<Button>()
    private val pages = ArrayList<View>()
    private var page = 0
    private var web: WebView? = null
    private var webUrl = ""

    // Гаусс
    private var winS = 10
    private var fromStep = false
    private var paused = false
    private var thick = 1                                // толщина столбика: 1 или 3 ступени датчика
    private val axisSel = intArrayOf(3, 3)              // [акселерометр, магнитометр]: 0..2 — ось, 3 — модуль
    private val axisBtns = Array(2) { ArrayList<Button>() }
    private lateinit var winLabel: TextView
    private lateinit var stepBtn: Button
    private lateinit var pauseBtn: Button
    private val gHead = arrayOfNulls<TextView>(2)
    private val gStat = arrayOfNulls<TextView>(2)
    private val gHist = arrayOfNulls<HistView>(2)
    private var tickN = 0

    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        val rig = Rig.get(this)                         // меряет служба; экран только показывает
        hub = rig.hub
        up = rig.up
        exp = rig.exp
        setContentView(build())
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)   // значок службы в шторке
    }

    override fun onResume() {
        super.onResume()
        if (Rig.wanted(this) && !SensorService.running) SensorService.start(this)
        web?.onResume()
        ui.post(tick)
    }

    override fun onPause() {                            // свернули или погасили экран — замеры идут дальше в службе
        super.onPause()
        ui.removeCallbacks(tick)
        web?.onPause()
    }

    // экран: гаснет сам (замеры идут) · не гаснет · притушен (не гаснет, яркость минимум) — горящий экран добавляет наводки
    private fun applyScreen() {
        if (screen > 0) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.attributes = window.attributes.apply { screenBrightness = if (screen == 2) 0.02f else -1f }
        screenBtn.text = arrayOf("☾ экран гаснет сам", "☀ экран не гаснет", "◐ экран притушен")[screen]
    }

    private fun batteryFree() = (getSystemService(POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(packageName)

    private fun dp(v: Float) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, resources.displayMetrics)

    private fun text(size: Float, color: Int = Ok.INK, mono: Boolean = false, bold: Boolean = false) = TextView(this).apply {
        setTextSize(TypedValue.COMPLEX_UNIT_SP, size)
        setTextColor(color)
        typeface = Typeface.create(if (mono) Typeface.MONOSPACE else Typeface.SANS_SERIF, if (bold) Typeface.BOLD else Typeface.NORMAL)
    }

    private fun btn(label: String, size: Float = 13f, onClick: () -> Unit) = Button(this).apply {
        text = label
        isAllCaps = false
        setTextSize(TypedValue.COMPLEX_UNIT_SP, size)
        minHeight = 0; minimumHeight = 0; minWidth = 0; minimumWidth = 0
        val p = dp(6f).toInt()
        setPadding(p * 2, p, p * 2, p)
        setOnClickListener { onClick() }
    }

    private fun mark(b: Button, on: Boolean) {
        b.setBackgroundColor(if (on) Ok.SEL else Ok.BTN)
        b.setTextColor(if (on) Ok.INK else Ok.DIM)
        b.typeface = Typeface.create(Typeface.SANS_SERIF, if (on) Typeface.BOLD else Typeface.NORMAL)
    }

    private fun card(vararg views: View, weight: Float = 0f) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setBackgroundColor(Ok.CARD)
        val p = dp(8f).toInt()
        setPadding(p, p, p, p)
        views.forEach { addView(it) }
        layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
            if (weight > 0) 0 else LinearLayout.LayoutParams.WRAP_CONTENT, weight).apply { topMargin = dp(6f).toInt() }
    }

    private fun row(vararg views: View) = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        views.forEach { addView(it) }
    }

    private fun fill(v: View, weight: Float = 1f) =
        v.apply { layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, weight) }

    private fun build(): View {
        status = text(14f, bold = true)
        expLine = text(13f)
        foot = text(11f, Ok.DIM)
        screenBtn = btn("", 12f) { screen = (screen + 1) % 3; applyScreen() }
        applyScreen()
        runBtn = btn("", 12f) {
            val on = !SensorService.running
            Rig.setWanted(this, on)
            if (on) SensorService.start(this) else SensorService.stop(this)
        }
        // «работать без ограничений батареи»: иначе неподвижный телефон с погашенным экраном Android усыпляет вместе с сетью
        batBtn = btn("🔋 без ограничений", 12f) {
            try {
                startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
            } catch (_: Exception) {
                startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            }
        }
        val tabRow = row()
        listOf("〽 датчики", "∩ Гаусс", "▦ планшетка").forEachIndexed { i, s ->
            val b = btn(s, 14f) { show(i) }
            tabs += b
            tabRow.addView(b, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply { marginEnd = dp(4f).toInt() })
        }
        pages += buildSensors()
        pages += buildGauss()
        pages += FrameLayout(this)                       // планшетка: WebView создаётся при первом показе
        val content = FrameLayout(this)
        pages.forEach { content.addView(it, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT)) }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Ok.BG)
            val p = dp(10f).toInt()
            setPadding(p, p, p, p)
            setOnApplyWindowInsetsListener { v, ins ->        // Android 15: окно до краёв — не залезать под панели
                val b = ins.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
                v.setPadding(p + b.left, p + b.top, p + b.right, p + b.bottom)
                ins
            }
            addView(card(status, expLine))
            addView(tabRow.apply {
                layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(6f).toInt() }
            })
            addView(fill(content))
            addView(row(foot.apply { layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f) }))
            addView(row(runBtn, batBtn, screenBtn).apply {
                for (i in 0 until childCount) getChildAt(i).layoutParams =
                    LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply { marginEnd = dp(4f).toInt() }
            })
        }
        show(0)
        return root
    }

    private fun buildSensors(): View {
        accHead = text(15f, bold = true)
        accVal = text(14f, mono = true)
        accSd = text(12f, Ok.DIM)
        accPlot = LanePlot(this, "мg")
        magHead = text(15f, bold = true)
        magVal = text(14f, mono = true)
        magSd = text(12f, Ok.DIM)
        magPlot = LanePlot(this, "мкТл")
        bg = text(13f)
        val accCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(accHead); addView(accVal); addView(accSd); addView(fill(accPlot))
        }
        val magCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(magHead); addView(magVal); addView(magSd); addView(fill(magPlot))
        }
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(card(accCard, weight = 1f))
            addView(card(magCard, weight = 1f))
            addView(card(bg))
        }
    }

    private fun buildGauss(): View {
        winLabel = text(13f, bold = true)
        val seek = SeekBar(this).apply {
            max = 59
            progress = winS - 1
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(s: SeekBar?, p: Int, user: Boolean) {
                    winS = p + 1
                    if (user && fromStep) { fromStep = false; mark(stepBtn, false) }
                    gaussLabels()
                }
                override fun onStartTrackingTouch(s: SeekBar?) {}
                override fun onStopTrackingTouch(s: SeekBar?) { refreshGauss() }
            })
        }
        stepBtn = btn("с начала шага", 12f) { fromStep = !fromStep; mark(stepBtn, fromStep); gaussLabels(); refreshGauss() }
        val thickBtn = btn("▮ 1 ступень", 12f) {}
        thickBtn.setOnClickListener {
            thick = if (thick == 1) 3 else 1
            thickBtn.text = if (thick == 1) "▮ 1 ступень" else "▮▮▮ 3 ступени"
            mark(thickBtn, thick == 3)
            refreshGauss()
        }
        mark(thickBtn, false)
        pauseBtn = btn("⏸ пауза", 12f) {
            paused = !paused
            pauseBtn.text = if (paused) "▶ обновлять" else "⏸ пауза"
            mark(pauseBtn, paused)
        }
        mark(stepBtn, false); mark(pauseBtn, false)
        val ctl = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(row(winLabel.apply { layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f) }, stepBtn, pauseBtn))
            addView(row(text(12f, Ok.DIM).apply {
                text = "столбик — ступень датчика; 3 ступени сглаживают его неровные ступени (у A26 — через одну на третью)"
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }, thickBtn))
            addView(row(text(12f, Ok.DIM).apply { text = "1 с" }, seek.apply {
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }, text(12f, Ok.DIM).apply { text = "60 с" }))
        }
        val panels = (0..1).map { k ->
            val names = if (k == 0) listOf("x", "y", "z", "|a|") else listOf("x", "y", "z", "|B|")
            val r = row()
            names.forEachIndexed { i, s ->
                val b = btn(s, 13f) { axisSel[k] = i; axisBtns[k].forEachIndexed { j, bb -> mark(bb, j == i) }; refreshGauss() }
                axisBtns[k] += b
                mark(b, i == axisSel[k])
                r.addView(b, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply { marginEnd = dp(3f).toInt() })
            }
            gHead[k] = text(14f, bold = true)
            gStat[k] = text(12f, mono = true)
            gHist[k] = HistView(this, if (k == 0) "мg" else "мкТл")
            card(LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                addView(gHead[k]); addView(r); addView(fill(gHist[k]!!)); addView(gStat[k])
            }, weight = 1f)
        }
        gaussLabels()
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(card(ctl))
            panels.forEach { addView(it) }
        }
    }

    private fun gaussLabels() {
        winLabel.text = if (fromStep) "окно: ${exp.stepWhat}" else "окно: последние $winS с"
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun show(i: Int) {
        page = i
        pages.forEachIndexed { j, v -> v.visibility = if (j == i) View.VISIBLE else View.GONE }
        tabs.forEachIndexed { j, b -> mark(b, j == i) }
        if (i == 2) {
            val holder = pages[2] as FrameLayout
            val w = web ?: WebView(this).also { wv ->
                wv.settings.javaScriptEnabled = true
                wv.settings.domStorageEnabled = true
                wv.settings.useWideViewPort = true          // планшетка свёрстана на широкий экран — показать целиком
                wv.settings.loadWithOverviewMode = true
                wv.settings.builtInZoomControls = true
                wv.settings.displayZoomControls = false
                wv.webViewClient = WebViewClient()
                wv.webChromeClient = WebChromeClient()       // confirm() планшетки — обычным окном
                holder.addView(wv, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
                web = wv
            }
            if (webUrl != up.url) {
                webUrl = up.url
                w.loadUrl(up.url + "/plan")
            }
        }
        if (i == 1) refreshGauss()
    }

    private val tick = object : Runnable {
        override fun run() {
            refresh()
            ui.postDelayed(this, 200)
        }
    }

    private fun refresh() {
        val now = SystemClock.elapsedRealtime()
        tickN++
        val fresh = up.ok && now - up.lastOkMs < 3000
        val how = if (up.url == Uplink.USB_URL) "по USB" else "по Wi-Fi " + up.url.removePrefix("http://").removeSuffix(":8090")
        status.text = if (fresh) "✓ на связи с сервером $how · принято ${up.sent} пакетов"
        else "✗ нет связи с сервером — ${if (up.lastErr.isNotEmpty()) up.lastErr else "ищу"} (USB-кабель к компьютеру с сервером или Wi-Fi той же сети)"
        status.setTextColor(if (fresh) Ok.BLUE else Ok.VERM)
        expLine.text = exp.line
        expLine.setTextColor(if (exp.running) Ok.YELLOW else Ok.DIM)
        val free = batteryFree()
        foot.text = "адрес для сервера ${up.mac} · версия ${BuildInfo.VERSION}\n" +
            (if (SensorService.running) "✓ меряет в фоне — экран можно гасить, приложение сворачивать" else "■ замеры остановлены — «▶ мерить»") +
            (if (free) " · ✓ батарея без ограничений" else " · ⚠ батарея с ограничениями: в глубоком сне Android может отключить сеть") +
            (if (up.accLost > 0 || up.magLost > 0) " · ⚠ потеряно отсчётов: ${up.accLost} + поле ${up.magLost}" else "")
        runBtn.text = if (SensorService.running) "■ стоп" else "▶ мерить"
        batBtn.visibility = if (free) View.GONE else View.VISIBLE
        when (page) {
            0 -> refreshSensors()
            1 -> if (tickN % 3 == 0) refreshGauss()
        }
    }

    private fun refreshSensors() {
        // акселерометр: последние 10 с на графике, разброс — за последнюю секунду
        val ahz = hub.accRate.hz
        accHead.text = if (hub.acc == null) "📐 акселерометра нет" else
            "📐 акселерометр · ${f(ahz, 0)} Гц · ступень ${f(1000.0 / hub.accLsb, 2)} мg" + if (hub.accClip > 0) " · ⚠ упирался в ±8 g" else ""
        val a = hub.accTail(max(50, (ahz * 10).roundToInt()))
        if (a[0].isNotEmpty()) {
            val last = (0..2).map { a[it].last().toDouble() }
            val mod = sqrt(last.sumOf { it * it })
            accVal.text = "x ${fs(last[0], 1)}  y ${fs(last[1], 1)}  z ${fs(last[2], 1)}  |a| ${f(mod, 1)} мg"
            val n1 = min(a[0].size, max(10, ahz.roundToInt()))
            val sd = (0..2).map { meanSd(a[it].copyOfRange(a[it].size - n1, a[it].size)).second }
            accSd.text = "разброс за 1 с: x ${f(sd[0], 2)} · y ${f(sd[1], 2)} · z ${f(sd[2], 2)} мg  (график — 10 с, отклонение от среднего)"
            accPlot.set(a)
        }

        // магнитометр
        val mhz = hub.magRate.hz
        magHead.text = if (hub.mag == null) "🧲 магнитометра нет" else
            "🧲 магнитометр · ${f(mhz, 0)} Гц · ступень ${f((hub.mag?.resolution ?: 0f).toDouble(), 2)} мкТл"
        val m = hub.magTail(max(20, (mhz * 10).roundToInt()))
        if (m[0].isNotEmpty()) {
            val last = (0..2).map { m[it].last().toDouble() }
            val mod = sqrt(last.sumOf { it * it })
            val earth = if (hub.magHasBias) sqrt((0..2).sumOf { val q = last[it] - hub.magBias[it]; q * q }) else null
            magVal.text = "x ${fs(last[0], 2)}  y ${fs(last[1], 2)}  z ${fs(last[2], 2)}  |B| ${f(mod, 1)} мкТл"
            val n1 = min(m[0].size, max(5, mhz.roundToInt()))
            val sd = (0..2).map { meanSd(m[it].copyOfRange(m[it].size - n1, m[it].size)).second }
            magSd.text = "разброс за 1 с: x ${f(sd[0], 3)} · y ${f(sd[1], 3)} · z ${f(sd[2], 3)} мкТл\n" +
                "без калибровки: в числах — и магниты самого телефона (постоянно), изменения поля честные" +
                (earth?.let { "; без них |B| ≈ ${f(it, 1)} мкТл (Земля ≈ 50)" } ?: "")
            magPlot.set(m)
        }

        // фон
        val em = up.em50
        val lines = ArrayList<String>()
        val nz = up.em50n
        lines += when {
            em == null -> "⚡ сеть 50 Гц: не видно — магнитометр ${f(mhz, 0)} Гц (нужно больше 105)"
            nz != null && em > 2 * nz -> "⚡ сеть 50 Гц в поле: ${f(em, 3)} мкТл — ✓ видна (шум рядом ${f(nz, 3)})"
            else -> "· сети 50 Гц не видно: ${f(em, 3)} мкТл — на уровне шума рядом (${nz?.let { f(it, 3) } ?: "—"})"
        } + if (up.magAvg > 1) "\n   (поправка: телефон сам усредняет поле по ${up.magAvg} замера — 50 Гц у него в ~4 раза слабее)" else ""
        lines += "〰 колебания поля за 2 с: " + (up.bac?.let { f(it, 3) + " мкТл" } ?: "—")
        lines += "📶 Wi-Fi " + (up.wifiRssi?.let { "$it дБм" } ?: "нет") + " · сотовая " + (up.cellDbm?.let { "$it дБм" } ?: if (up.noSim) "— (нет SIM-карты)" else "—")
        lines += "🔋 " + (up.battPct?.let { "$it %" } ?: "—") + " · " + (up.battTemp?.let { f(it, 1) + " °C" } ?: "—") +
            if (up.plugged) " · ⚡ заряжается" else " · ⚠ не на зарядке"
        bg.text = lines.joinToString("\n")
    }

    /** Гаусс: выбранный ряд за окно (последние N с или с начала шага), гистограмма и форма. */
    private fun refreshGauss() {
        gaussLabels()
        if (paused) return
        for (k in 0..1) {
            val mag = k == 1
            val hz = if (mag) hub.magRate.hz else hub.accRate.hz
            val head = if (mag) hub.magHead else hub.accHead
            val n = if (fromStep) (head - (if (mag) exp.stepMag else exp.stepAcc)).toInt()
            else (winS * max(1.0, hz)).roundToInt()
            val ax = axisSel[k]
            val v = hub.series(mag, ax, max(2, n))
            val sh = shape(v)
            val nm = if (mag) listOf("x", "y", "z", "|B|")[ax] else listOf("x", "y", "z", "|a|")[ax]
            val unit = if (mag) "мкТл" else "мg"
            val dur = if (hz > 0) v.size / hz else 0.0
            gHead[k]!!.text = (if (mag) "🧲 поле " else "📐 ускорение ") + nm + " · " + f(dur, 1) + " с, отсчётов ${sh.n}"
            val dd = if (mag) 3 else 2
            gStat[k]!!.text = "среднее ${f(sh.mean, dd)} $unit · σ ${f(sh.sd, dd + 1)}\n" +
                "асимметрия ${f(sh.skew, 2)} · эксцесс ${f(sh.kurt, 2)} → ${sh.word()}"
            gStat[k]!!.setTextColor(if (sh.word().startsWith("✓")) Ok.BLUE else Ok.ORANGE)
            // ступень датчика: у осей акселерометра — 1/4096 g; у поля — узнать по самим замерам
            val q = when {
                !mag -> 1000.0 / hub.accLsb                 // у |a| почти та же ступень: в нём главная — ось z
                ax < 3 -> quantum(v)
                else -> 0.0                                  // |B| складывается из трёх осей поровну — решётки нет
            }
            gHist[k]!!.set(v, sh, q, thick)
        }
    }

    /** Наименьший шаг между значениями (ступень датчика), если значения действительно ступенчатые. */
    private fun quantum(v: FloatArray): Double {
        if (v.size < 50) return 0.0
        val s = v.copyOfRange(max(0, v.size - 2000), v.size).sorted()
        var q = Double.MAX_VALUE
        for (i in 1 until s.size) {
            val d = (s[i] - s[i - 1]).toDouble()
            if (d > 1e-5) q = min(q, d)
        }
        return if (q == Double.MAX_VALUE) 0.0 else q
    }
}

/**
 * Гистограмма отклонений с гауссом той же середины и разброса: столбики — сколько отсчётов,
 * штриховая линия — сколько было бы у гаусса. Столбики выровнены по ступени датчика (иначе
 * у ступенчатых данных вылезает «гребёнка»). Подписи: −3σ, среднее, +3σ; за краем ±4,5σ — счёт.
 */
class HistView(ctx: Context, private val unit: String) : View(ctx) {
    private val d = ctx.resources.displayMetrics.density
    private var counts = IntArray(0)
    private var lo = 0.0
    private var w = 1.0
    private var sh: Shape? = null
    private var outside = 0
    private val bar = Paint().apply { color = Ok.BLUE }
    private val curve = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Ok.ORANGE; style = Paint.Style.STROKE; strokeWidth = 2f * d
        pathEffect = DashPathEffect(floatArrayOf(7f * d, 4f * d), 0f)
    }
    private val axis = Paint().apply { color = 0xFF2C343D.toInt(); strokeWidth = 1f }
    private val label = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Ok.DIM; textSize = 11f * d; typeface = Typeface.MONOSPACE }

    fun set(v: FloatArray, s: Shape, q: Double, mult: Int) {
        sh = s
        if (v.size < 2 || s.sd <= 0) { counts = IntArray(0); invalidate(); return }
        val span = 4.5 * s.sd
        var bw = 2 * span / 70
        var start = s.mean - span
        if (q > 0) {                                     // ступенчатые данные: столбик — 1 или 3 ступени
            var m = mult
            while (2 * span / (q * m) > 300) m += 3       // размах огромный (толчки) — столбиков не больше ~300
            bw = q * m
            // решётка может быть сдвинута (у |a|): сдвиг — медиана остатков от деления на ступень
            val r = FloatArray(min(v.size, 3000)) { i -> val x = v[v.size - 1 - i] / q; (x - floor(x)).toFloat() }
            r.sort()
            val off = r[r.size / 2] * q
            start = (floor((s.mean - span - off) / q) - 0.5) * q + off
        }
        val nb = max(3, ceil((s.mean + span - start) / bw).toInt())
        val c = IntArray(nb)
        var out = 0
        for (x in v) {
            val i = floor((x - start) / bw).toInt()
            if (i in 0 until nb) c[i]++ else out++
        }
        counts = c; lo = start; w = bw; outside = out
        invalidate()
    }

    override fun onDraw(cv: Canvas) {
        val s = sh ?: return
        if (counts.isEmpty()) {
            cv.drawText("мало отсчётов", 8f * d, height / 2f, label)
            return
        }
        val top = 14f * d
        val bottom = height - 16f * d
        val nb = counts.size
        val xOf = { x: Double -> ((x - lo) / (w * nb) * width).toFloat() }
        val n = counts.sum() + outside
        val pdfMax = n * w / (s.sd * sqrt(2 * Math.PI))
        val yMax = max(counts.max().toDouble(), pdfMax) * 1.05
        val yOf = { c: Double -> (bottom - c / yMax * (bottom - top)).toFloat() }
        for (i in 0 until nb) {
            if (counts[i] == 0) continue
            val x0 = xOf(lo + i * w)
            val x1 = xOf(lo + (i + 1) * w)
            cv.drawRect(x0 + 0.5f, yOf(counts[i].toDouble()), max(x0 + 1.5f, x1 - 0.5f), bottom, bar)
        }
        val p = Path()
        for (k in 0..200) {
            val x = lo + k * (w * nb) / 200
            val z = (x - s.mean) / s.sd
            val y = yOf(pdfMax * exp(-0.5 * z * z))
            if (k == 0) p.moveTo(xOf(x), y) else p.lineTo(xOf(x), y)
        }
        cv.drawPath(p, curve)
        cv.drawLine(0f, bottom, width.toFloat(), bottom, axis)
        val fmt = { x: Double -> String.format(Locale.forLanguageTag("ru"), if (abs(s.sd) < 0.1) "%.3f" else "%.2f", x) }
        for (k in -3..3 step 3) {
            val x = s.mean + k * s.sd
            val px = xOf(x)
            cv.drawLine(px, bottom, px, bottom + 4f * d, axis)
            val t = if (k == 0) fmt(x) else (if (k > 0) "+3σ" else "−3σ")
            val tw = label.measureText(t)
            cv.drawText(t, (px - tw / 2).coerceIn(0f, width - tw), height - 2f * d, label)
        }
        cv.drawText("▮ отсчёты  ┅ гаусс  · $unit" + if (outside > 0) "  · за ±4,5σ: $outside" else "", 2f * d, label.textSize, label)
    }
}

/**
 * Три полосы — x, y, z — одна под другой (цвет не нужен, чтобы различать оси): в каждой — размах
 * отклонений от среднего по столбцам экрана (сверху — наибольшее, снизу — наименьшее за столбец),
 * масштаб общий, подписан.
 */
class LanePlot(ctx: Context, private val unit: String) : View(ctx) {
    private var data: Array<FloatArray>? = null
    private val d = ctx.resources.displayMetrics.density
    private val pen = Paint().apply { strokeWidth = 1.2f * d; isAntiAlias = false }
    private val axis = Paint().apply { color = 0xFF2C343D.toInt(); strokeWidth = 1f }
    private val label = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Ok.DIM; textSize = 11f * d; typeface = Typeface.MONOSPACE }
    private val colors = intArrayOf(Ok.BLUE, Ok.ORANGE, Ok.GREEN)

    fun set(v: Array<FloatArray>) {
        data = v
        invalidate()
    }

    override fun onDraw(c: Canvas) {
        val v = data ?: return
        val n = v[0].size
        if (n < 2) return
        val left = 22f * d
        val w = width - left
        val laneH = height / 3f
        val cols = max(1, min(w.toInt(), n))
        val lo = Array(3) { FloatArray(cols) }
        val hi = Array(3) { FloatArray(cols) }
        var span = 0f
        for (j in 0..2) {
            var s = 0.0
            for (x in v[j]) s += x
            val mean = (s / n).toFloat()
            for (k in 0 until cols) {
                val a = (k.toLong() * n / cols).toInt()
                val b = max(a + 1, ((k + 1).toLong() * n / cols).toInt())
                var mn = Float.MAX_VALUE
                var mx = -Float.MAX_VALUE
                for (i in max(0, a - 1) until min(b, n)) {        // с соседним отсчётом — линия без разрывов
                    val x = v[j][i] - mean
                    mn = min(mn, x); mx = max(mx, x)
                }
                lo[j][k] = mn; hi[j][k] = mx
                span = max(span, max(abs(mn), abs(mx)))
            }
        }
        if (span <= 0f) span = 1f
        for (j in 0..2) {
            val mid = laneH * j + laneH / 2
            c.drawLine(left, mid, width.toFloat(), mid, axis)
            c.drawText("xyz"[j].toString(), 4f * d, mid + 4f * d, label)
            pen.color = colors[j]
            val k2 = (laneH / 2 - 2f * d) / span
            for (k in 0 until cols) {
                val x = left + k * w / cols
                c.drawLine(x, mid - hi[j][k] * k2, x, mid - lo[j][k] * k2 + 1f, pen)
            }
        }
        c.drawText("±" + String.format(Locale.forLanguageTag("ru"), if (span < 1) "%.3f" else "%.1f", span) + " $unit",
            left + 2f * d, label.textSize, label)
    }
}
