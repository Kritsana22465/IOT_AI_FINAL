# IoT Vision Dashboard

ระบบตรวจจับอุปกรณ์ด้วยกล้องและ YOLO โดยใช้ IR Sensor จาก Arduino UNO R4 WiFi เป็นตัวเริ่มรอบการจับภาพ

## Flow การทำงาน

1. Arduino ส่ง `object_present` ผ่าน HTTPS ไปยัง Dashboard
2. Dashboard สร้างคำขอจับภาพหนึ่งรอบ
3. หน้าเว็บเปิดกล้องของเบราว์เซอร์และส่งภาพให้ API วิเคราะห์
4. ระบบบันทึกภาพ เวลา และชื่ออุปกรณ์ลง PostgreSQL
5. หน้าเว็บปิดกล้องอัตโนมัติหลังบันทึกสำเร็จ
6. เมื่อ Arduino ส่ง `false` ต่อเนื่องครบ 10 วินาที ระบบจึงพร้อมรับรอบใหม่

สถานะกันการบันทึกซ้ำถูกเก็บใน PostgreSQL จึงไม่เริ่มรอบใหม่เพียงเพราะ container restart

## โครงสร้าง cloud แบบไม่เสียค่าใช้จ่าย

ใช้ Oracle Cloud Always Free VM แบบ ARM ใน region `ap-singapore-1`:

- VM.Standard.A1.Flex: 2 OCPU, RAM 12 GB
- Docker Compose: Dashboard, PostgreSQL และ Caddy HTTPS
- PostgreSQL และรูปภาพเก็บบน disk ของ VM
- Caddy ขอใบรับรอง HTTPS ให้อัตโนมัติ
- GitHub Actions deploy ทุกครั้งที่ push เข้า `main`

วิธีนี้ไม่ใช้ AWS, S3 หรือบริการเสียเงินเพิ่มเติม แต่ Always Free ยังขึ้นกับ quota และ capacity ของ Oracle Cloud

## รันในเครื่องด้วย Docker

```powershell
docker compose up --build
```

เปิด `http://localhost:8000` แล้วอนุญาตสิทธิ์กล้อง รูป local อยู่ที่ `data/snapshots` และข้อมูลอยู่ใน Docker volume

## ตั้งค่า Arduino

แก้ค่าก่อน Upload ในไฟล์ `Project_IoT.ino`:

```cpp
const char WIFI_SSID[] = "ชื่อ Wi-Fi";
const char WIFI_PASSWORD[] = "รหัสผ่าน Wi-Fi";
const char API_HOST[] = "โดเมน HTTPS ของ Dashboard";
const char API_KEY[] = "ค่าเดียวกับ ARDUINO_API_KEY";
```

ใช้ `WiFiSSLClient` และพอร์ต `443` ห้ามใช้ `localhost`, `127.0.0.1` หรือ IP ภายในเครื่อง เพราะ Arduino ต้องเข้าถึง VM ผ่านอินเทอร์เน็ต

อย่า commit ค่า Wi-Fi หรือ API key จริงลง GitHub

## Deploy Oracle Cloud ครั้งแรก

สร้าง VM ด้วย Ubuntu ARM และติดตั้ง Docker Compose จากนั้นเตรียมโฟลเดอร์ `/opt/iot-ai` บน VM โดยให้ user `ubuntu` ใช้ SSH ได้

สร้างไฟล์ SSH สำหรับ GitHub Actions บนเครื่องของคุณ แล้วเพิ่ม public key เข้า VM:

```powershell
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\iot-oci -C iot-ai-oci
Get-Content $env:USERPROFILE\.ssh\iot-oci.pub
```

ตั้งค่า GitHub repository secrets:

- `OCI_HOST`: public IP ของ VM
- `OCI_USER`: `ubuntu`
- `OCI_SSH_PRIVATE_KEY`: เนื้อหาไฟล์ `iot-oci` (private key)
- `DOMAIN`: hostname ที่ชี้มาที่ public IP ของ VM เช่น `123.123.123.123.nip.io` หรือโดเมนของคุณ
- `POSTGRES_PASSWORD`: รหัส PostgreSQL ที่ยาวและสุ่ม
- `ARDUINO_API_KEY`: API key ที่จะใส่ใน Arduino

จากนั้น push branch `main` ไฟล์ `.github/workflows/deploy-oci.yml` จะทำงานโดยอัตโนมัติ:

- ส่ง source code ไปยัง VM
- สร้าง Docker image บน VM ARM
- อัปเดต Dashboard และ PostgreSQL โดยคงข้อมูลเดิมไว้
- เปิด HTTPS ผ่าน Caddy
- ตรวจสอบ `/api/status` หลัง deploy

บน VM ให้สร้างไฟล์ `Caddyfile` จากไฟล์ใน repository และเปิด security list/security group เฉพาะพอร์ต `22`, `80`, `443` เท่านั้น

หน้าเว็บ cloud ต้องเปิดผ่าน HTTPS เพื่อให้เบราว์เซอร์อนุญาตกล้อง

## Train model

```powershell
py -m pip install -r requirements.txt
py train.py
```

โมเดลที่ใช้งานคือ `runs/object-detection/iot-components/weights/best.pt`

## หยุดระบบ local

```powershell
docker compose down
```

คำสั่งนี้ยังเก็บ PostgreSQL volume และ snapshot ไว้ ถ้าต้องการลบข้อมูล local ทั้งหมดจึงค่อยใช้ `docker compose down -v`

## ข้อจำกัดของข้อมูล

ชุดข้อมูลมีภาพต้นฉบับเพียงประมาณ 9–10 ภาพต่อคลาส โมเดลจึงเหมาะสำหรับต้นแบบ ควรเพิ่มภาพจริงหลายมุมและหลายสภาพแสงก่อนใช้งานจริง
