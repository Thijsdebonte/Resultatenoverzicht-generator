#!/bin/bash
# Faam Wervingsrapport Generator — lokaal starten
# Voer dit script uit om de app op je eigen computer te draaien.

echo "📦 Dependencies installeren..."
pip install flask fpdf2 gunicorn

echo ""
echo "🚀 App starten op http://localhost:5000"
echo "   Druk op Ctrl+C om te stoppen."
echo ""

export DEBUG=true
python app.py
