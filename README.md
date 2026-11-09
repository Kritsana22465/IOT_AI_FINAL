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

## โครงสร้าง AWS

ใช้ AWS Project ชื่อ `Future Startup` ใน region `ap-southeast-2` โดยรัน Docker บน EC2 และใช้ PostgreSQL ใน Docker volume เดียวกับเครื่อง:

- EC2 Linux ขนาดที่อยู่ใน Free Tier/เครดิตของบัญชี เช่น `t3.small` เพื่อให้ YOLO มีหน่วยความจำพอ
- Docker Compose: Dashboard, PostgreSQL และ Caddy HTTPS
- PostgreSQL และรูปภาพเก็บบน EBS volume ของ EC2
- ใช้ hostname แบบ `nip.io` จาก Elastic IP ได้ จึงไม่ต้องซื้อโดเมนเพิ่ม
- GitHub Actions ตรวจสอบโค้ดและ deploy ทุกครั้งที่ push เข้า `main`

AWS แสดงเครดิตคงเหลือและวันหมดอายุในหน้า Console Home ของบัญชีนี้แล้ว ควรตรวจสอบ Billing เป็นระยะ เพราะการใช้ฟรีขึ้นกับ Free Tier/เครดิตและชนิด instance ที่เลือก

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

## Deploy AWS ครั้งแรก

สร้าง EC2 ด้วย Ubuntu 24.04 x86 และเลือก user data จากไฟล์ `infra/cloud-init-aws.yaml` เพื่อให้ติดตั้ง Docker อัตโนมัติ จากนั้นเปิด Security Group เฉพาะพอร์ต `22`, `80`, `443` และผูก Elastic IP เข้ากับ instance

ตั้งค่า GitHub repository secrets:

- `AWS_HOST`: Elastic IP ของ EC2
- `AWS_USER`: `ubuntu`
- `AWS_SSH_PRIVATE_KEY`: private key ของ EC2 key pair
- `DOMAIN`: เช่น `12.34.56.78.nip.io` โดยแทนด้วย Elastic IP จริง หรือใช้โดเมนของคุณ
- `POSTGRES_PASSWORD`: รหัส PostgreSQL ที่ยาวและสุ่ม
- `ARDUINO_API_KEY`: API key ที่จะใส่ใน Arduino

จากนั้น workflow `.github/workflows/deploy-aws.yml` จะทำงานอัตโนมัติ:

- ตรวจสอบ Python และ Docker Compose
- ส่ง source code ไปยัง EC2
- build และ restart Dashboard, PostgreSQL และ Caddy
- เปิด HTTPS ผ่าน Caddy
- ตรวจสอบ `/api/status` หลัง deploy

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
