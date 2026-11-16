plugins {
    // AGP 8.13.0 (подъём с 8.7.3, решение пользователя 16.11): compileSdk 37
    // требует AGP с поддержкой минорной схемы SDK (37.0/37.1/37.2 — «плоского»
    // пакета platforms;android-37 в репо больше нет), AGP 8.7.3 знал только
    // платформу до 35; 8.13.0 — минимальная поддерживающая, потолок матрицы
    // KGP 2.3.x (AGP ≤ 8.13.0); AGP 9.x (Gradle 9.x) не трогаем.
    id("com.android.application") version "8.13.0" apply false
    id("org.jetbrains.kotlin.android") version "2.3.21" apply false
}
