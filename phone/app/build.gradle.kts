plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "org.wheel2.phone"
    compileSdk = 35

    defaultConfig {
        applicationId = "org.wheel2.phone"
        minSdk = 29
        targetSdk = 35
        versionCode = 2                  // 1.1: замеры в службе (экран можно гасить), время замеров поля
        versionName = "1.1"
    }

    buildTypes {
        debug { isMinifyEnabled = false }
        release { isMinifyEnabled = false }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions { jvmTarget = "17" }
}
