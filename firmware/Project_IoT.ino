#include <WiFiS3.h>
#include <stdlib.h>

const int IR_PIN = 4;
const int BUZZER_PIN = 5;
const bool BUZZER_IS_ACTIVE = true;  // false สำหรับ passive buzzer
const bool BUZZER_ACTIVE_HIGH = true;  // false สำหรับ active module แบบ LOW trigger
const unsigned int BUZZER_FREQUENCY_HZ = 2000;
const unsigned long WAIT_BEEP_MS = 150;
const unsigned long WAIT_INTERVAL_MS = 1500;
const unsigned long COMPLETE_BEEP_MS = 1000;
const unsigned long QUEUE_STATE_TIMEOUT_MS = 10000;

// เปลี่ยนเป็น false หาก IR sensor ของคุณส่งค่า LOW เมื่อมีวัตถุ
const bool IR_ACTIVE_HIGH = true;

// กรอกค่าเฉพาะเครื่องก่อน Upload และอย่า commit ค่าจริงลง GitHub
const char WIFI_SSID[] = "YOUR_WIFI_NAME";
const char WIFI_PASSWORD[] = "YOUR_WIFI_PASSWORD";
const char API_HOST[] = "d1klutfjlhnmro.cloudfront.net";
const int API_PORT = 443;
const char API_PATH[] = "/api/arduino/ir";
const char API_KEY[] = "YOUR_ARDUINO_API_KEY";
const unsigned long REPORT_INTERVAL_MS = 3000;
const unsigned long RETRY_INTERVAL_MS = 1500;
const unsigned long HTTP_TIMEOUT_MS = 8000;

WiFiSSLClient client;
bool hasSentStatus = false;
bool lastSentObjectPresent = false;
unsigned long lastReportAt = 0;
unsigned long lastAttemptAt = 0;

enum class BuzzerSound { Silent, Waiting, Completed };
void startBuzzer(BuzzerSound sound, unsigned long now);

BuzzerSound buzzerSound = BuzzerSound::Silent;
bool queueWaiting = false;
bool completionPending = false;
bool hasCompletionBaseline = false;
unsigned long long lastCompletedQueueId = 0;
unsigned long lastQueueStateAt = 0;
unsigned long buzzerStartedAt = 0;
unsigned long lastWaitingBeepAt = 0;

void stopBuzzer() {
  if (!BUZZER_IS_ACTIVE) {
    noTone(BUZZER_PIN);
  }
  digitalWrite(BUZZER_PIN, BUZZER_IS_ACTIVE && !BUZZER_ACTIVE_HIGH ? HIGH : LOW);
  buzzerSound = BuzzerSound::Silent;
}

void startBuzzer(BuzzerSound sound, unsigned long now) {
  buzzerSound = sound;
  buzzerStartedAt = now;
  if (BUZZER_IS_ACTIVE) {
    digitalWrite(BUZZER_PIN, BUZZER_ACTIVE_HIGH ? HIGH : LOW);
  } else {
    tone(BUZZER_PIN, BUZZER_FREQUENCY_HZ);
  }
}

void updateBuzzer(bool objectPresent, unsigned long now) {
  const bool shouldWait = queueWaiting && !objectPresent
    && now - lastQueueStateAt < QUEUE_STATE_TIMEOUT_MS;
  if (buzzerSound == BuzzerSound::Completed
      && now - buzzerStartedAt < COMPLETE_BEEP_MS) {
    return;
  }
  if (buzzerSound == BuzzerSound::Waiting && shouldWait && !completionPending
      && now - buzzerStartedAt < WAIT_BEEP_MS) {
    return;
  }
  if (buzzerSound != BuzzerSound::Silent) {
    stopBuzzer();
  }

  if (completionPending) {
    completionPending = false;
    lastWaitingBeepAt = now;
    startBuzzer(BuzzerSound::Completed, now);
  } else if (shouldWait && now - lastWaitingBeepAt >= WAIT_INTERVAL_MS) {
    lastWaitingBeepAt = now;
    startBuzzer(BuzzerSound::Waiting, now);
  }
}

bool readResponseLine(String &line, unsigned long startedAt) {
  line = "";
  while (millis() - startedAt < HTTP_TIMEOUT_MS) {
    if (!client.available()) {
      if (!client.connected()) return false;
      delay(10);
      continue;
    }
    const int character = client.read();
    if (character == '\n') {
      line.trim();
      return true;
    }
    if (character >= 0 && line.length() < 256) {
      line += static_cast<char>(character);
    }
  }
  return false;
}

void readBuzzerHeaders(unsigned long startedAt) {
  bool waiting = false;
  String completedQueue;
  for (String header; readResponseLine(header, startedAt) && header.length();) {
    const int colon = header.indexOf(':');
    if (colon < 0) continue;
    const String name = header.substring(0, colon);
    String value = header.substring(colon + 1);
    value.trim();
    if (name.equalsIgnoreCase("X-IoT-Queue-Waiting")) {
      waiting = value == "1";
    } else if (name.equalsIgnoreCase("X-IoT-Completed-Queue")) {
      completedQueue = value;
    }
  }

  const unsigned long now = millis();
  if (waiting && (!queueWaiting || now - lastQueueStateAt >= QUEUE_STATE_TIMEOUT_MS)) {
    lastWaitingBeepAt = now - WAIT_INTERVAL_MS;
  }
  queueWaiting = waiting;
  lastQueueStateAt = now;

  if (completedQueue.length() && completedQueue[0] >= '0' && completedQueue[0] <= '9') {
    char *end = nullptr;
    const unsigned long long completedId = strtoull(completedQueue.c_str(), &end, 10);
    if (*end == '\0') {
      if (hasCompletionBaseline && completedId > lastCompletedQueueId) {
        completionPending = true;
      }
      if (completedId > lastCompletedQueueId) lastCompletedQueueId = completedId;
      hasCompletionBaseline = true;
    }
  }
}

void connectWiFi() {
  while (WiFi.status() != WL_CONNECTED) {
    Serial.print("กำลังเชื่อมต่อ Wi-Fi: ");
    Serial.println(WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    const unsigned long startedAt = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - startedAt < 10000) {
      delay(500);
      Serial.print(".");
    }
    Serial.println();
  }

  Serial.print("Wi-Fi connected, IP: ");
  Serial.println(WiFi.localIP());
}

bool sendIRStatus(bool objectPresent) {
  const String payload = String("{\"object_present\":")
    + (objectPresent ? "true" : "false") + "}";

  for (int attempt = 1; attempt <= 2; attempt++) {
    client.stop();
    client.setConnectionTimeout(HTTP_TIMEOUT_MS);
    client.setTimeout(HTTP_TIMEOUT_MS);

    if (!client.connect(API_HOST, API_PORT)) {
      if (attempt == 2) {
        Serial.print("เชื่อมต่อ Dashboard API ไม่สำเร็จ · WiFi status: ");
        Serial.print(WiFi.status());
        Serial.print(" · RSSI: ");
        Serial.println(WiFi.RSSI());
      } else {
        delay(250);
      }
      continue;
    }

    client.print("POST ");
    client.print(API_PATH);
    client.println(" HTTP/1.1");
    client.print("Host: ");
    client.println(API_HOST);
    client.print("X-API-Key: ");
    client.println(API_KEY);
    client.println("Content-Type: application/json");
    client.print("Content-Length: ");
    client.println(payload.length());
    client.println("Connection: close");
    client.println();
    client.print(payload);

    const unsigned long startedAt = millis();
    String statusLine;
    const bool gotStatus = readResponseLine(statusLine, startedAt);

    if (gotStatus && (statusLine.startsWith("HTTP/1.1 2")
      || statusLine.startsWith("HTTP/2 2"))) {
      readBuzzerHeaders(startedAt);
      client.stop();
      return true;
    }

    client.stop();
    Serial.print("Dashboard ตอบกลับ: ");
    Serial.println(statusLine.length() ? statusLine : "ไม่มี response");
    if (statusLine.startsWith("HTTP/1.1 4")
      || statusLine.startsWith("HTTP/2 4")) {
      return false;
    }
    delay(250);
  }
  return false;
}

void setup() {
  Serial.begin(115200);
  pinMode(IR_PIN, INPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  stopBuzzer();
  connectWiFi();
}

void loop() {
  const int sensorValue = digitalRead(IR_PIN);
  const bool objectPresent = IR_ACTIVE_HIGH
    ? sensorValue == HIGH
    : sensorValue == LOW;

  const unsigned long now = millis();
  updateBuzzer(objectPresent, now);
  // Wi-Fi อาจบล็อกชั่วคราว จึงส่งข้อมูลเฉพาะช่วงที่ buzzer เงียบ
  if (buzzerSound != BuzzerSound::Silent) {
    delay(10);
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    queueWaiting = false;
    connectWiFi();
    return;
  }

  const bool changed = !hasSentStatus || objectPresent != lastSentObjectPresent;
  const bool heartbeatDue = now - lastReportAt >= REPORT_INTERVAL_MS;
  const bool retryDue = now - lastAttemptAt >= RETRY_INTERVAL_MS;
  if ((changed || heartbeatDue) && retryDue) {
    Serial.print("IR object_present: ");
    Serial.println(objectPresent ? "true" : "false");
    lastAttemptAt = now;
    if (sendIRStatus(objectPresent)) {
      hasSentStatus = true;
      lastSentObjectPresent = objectPresent;
      lastReportAt = millis();
      Serial.println("ส่งสถานะ IR สำเร็จ");
    }
  }

  delay(10);
}
