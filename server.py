
# server.py
# G-DSP Final Version (Vworld Proxy + Static Site)

from flask import Flask, request, jsonify, send_from_directory
import requests

app = Flask(__name__, static_folder=".")

@app.after_request
def cors(res):
    res.headers["Access-Control-Allow-Origin"] = "*"
    res.headers["Access-Control-Allow-Headers"] = "Content-Type"
    res.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return res

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

# Vworld proxy (CORS 해결)
@app.route("/api/address")
def address():
    addr = request.args.get("q")

    if not addr:
        return jsonify({"error":"address missing"})

    url = "https://api.vworld.kr/req/search"

    params = {
        "service":"search",
        "request":"search",
        "version":"2.0",
        "type":"address",
        "category":"road",
        "query":addr,
        "key":" 16B90D39-90BB-3197-987A-54983A46F250"
    }

    r = requests.get(url,params=params)
    return jsonify(r.json())

if __name__ == "__main__":
    print("G-DSP server running")
    print("http://localhost:5050")
    app.run(host="0.0.0.0",port=5050,debug=True)
