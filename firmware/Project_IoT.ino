#include <WiFiS3.h>

const int IR_PIN = 4;

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
    client.println(payload);

    const unsigned long startedAt = millis();
    while (!client.available()
      && client.connected()
      && millis() - startedAt < HTTP_TIMEOUT_MS) {
      delay(10);
    }

    String statusLine = client.available()
      ? client.readStringUntil('\n')
      : "";
    statusLine.trim();
    client.stop();

    if (statusLine.startsWith("HTTP/1.1 2")
      || statusLine.startsWith("HTTP/2 2")) {
      return true;
    }

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
  connectWiFi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  const int sensorValue = digitalRead(IR_PIN);
  const bool objectPresent = IR_ACTIVE_HIGH
    ? sensorValue == HIGH
    : sensorValue == LOW;

  Serial.print("IR object_present: ");
  Serial.println(objectPresent ? "true" : "false");

  const unsigned long now = millis();
  const bool changed = !hasSentStatus || objectPresent != lastSentObjectPresent;
  const bool heartbeatDue = now - lastReportAt >= REPORT_INTERVAL_MS;
  const bool retryDue = now - lastAttemptAt >= RETRY_INTERVAL_MS;
  if ((changed || heartbeatDue) && retryDue) {
    lastAttemptAt = now;
    if (sendIRStatus(objectPresent)) {
      hasSentStatus = true;
      lastSentObjectPresent = objectPresent;
      lastReportAt = millis();
      Serial.println("ส่งสถานะ IR สำเร็จ");
    }
  }

  delay(100);
}
