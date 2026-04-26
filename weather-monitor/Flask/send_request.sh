#curl -X POST http://127.0.0.1:8080/send-to-bigquery \
#-H "Content-Type: application/json" \
#-d '{
#  "passwd": "<YOUR_PSWD>",
#  "values": {
#    "date": "2025-03-13",
#    "time": "16:30:00", 
#    "indoor_temp": 23,  
#    "indoor_humidity": 67
#  }
#}'

curl -X POST http://127.0.0.1:8080/get_outdoor_weather \
-H "Content-Type: application/json" \
-d '{"passwd": "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4", "city": "Paris"}'

curl -X POST http://127.0.0.1:8080/get_forecast \
-H "Content-Type: application/json" \
-d '{"passwd": "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4", "city": "Paris"}'