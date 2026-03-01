# Email QC System
Testing Email HTMl against the design file.

Goal - 
- To see if the email created in Klaviyo is correct


### Install Ollama if needed: https://ollama.com
ollama serve            # start Ollama (in a separate terminal or background)
ollama pull llama3.2-vision   # download the model (one-time)


### To install
cd /Users/rahi/Sites/email-test
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium 


### To Start
cd /Users/rahi/Sites/email-test
source venv/bin/activate
python app.py



 http://localhost:5001 


 OLLAMA_MODEL=llava python app.py

