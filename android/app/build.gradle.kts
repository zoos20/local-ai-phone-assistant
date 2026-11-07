plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "ru.gemma.assistant"
    compileSdk = 34

    defaultConfig {
        applicationId = "ru.gemma.assistant"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    packaging {
        jniLibs.useLegacyPackaging = true
    }
}

dependencies {
    // Оффлайн wake word + распознавание (русскоязычная модель качается отдельно)
    implementation("com.alphacephei:vosk-android:0.3.75")
    implementation("androidx.core:core-ktx:1.13.1")
    // Тема приложения (Theme.Gemma на базе Material3) + виджеты
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    // WebSocket-стриминг аудио на сервер во время речи (фаза 6, п. 1)
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // Характеризационные юнит-тесты (распознавание, фонетика, RMS, wake)
    testImplementation("junit:junit:4.13.2")
    // org.json в unit-тестах: android.jar замокан, нужна реальная реализация
    // для Updater.parseManifest (штатный workaround)
    testImplementation("org.json:json:20240303")
}
