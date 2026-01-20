import requests
import time
import os
import hashlib
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
    
def initialize_services():
    # Initialize Firestore
    cred_path = os.getenv('FIREBASE_SERVICE_ACCOUNT_PATH')
    db = None
    if cred_path and os.path.exists(cred_path):
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
    else:
        print("Firestore credentials not found. Skipping DB upload.")

    # Initialize Gemini AI
    ai_key = os.getenv('GEMINI_API_KEY')
    model = None
    if ai_key:
        genai.configure(api_key=ai_key)
        # Initialize model once here
        model = genai.GenerativeModel(
            model_name='gemini-2.5-flash',
            system_instruction="You are a sports news editor. Summarize articles into exactly two concise, engaging sentences for a mobile app feed."
        )
    
    return db, model

def summarize_article(model, title, description):
    prompt = f"Title: {title}\nDescription: {description}"
    response = model.generate_content(prompt)
    return response.text.strip()

def scrape_article_content(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            # Extract text from paragraphs, filtering out very short snippets
            paragraphs = soup.find_all('p')
            text = " ".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 20])
            
            # Trys to find an image via OpenGraph tags
            og_image = soup.find("meta", property="og:image")
            image_url = og_image["content"] if og_image else None

            # Trys to find the author in meta tags
            author_tag = soup.find("meta", name="author") or soup.find("meta", property="article:author")
            author = author_tag["content"] if author_tag else None
            return text[:5000], image_url, author # Return first 5k chars for efficiency
    except:
        pass
    return None, None, None

def get_news_articles(api_key, db, model):
    if not model:
        print("Error: AI model not initialized. Stopping.")
        return
    
    # Use /everything instead of /top-headlines to ensure we find articles even during slow news cycles
    url = "https://newsapi.org/v2/everything"
    params = {
        "qInTitle": "NBA OR MLB OR NFL",
        "domains": "espn.com,nfl.com,nba.com,mlb.com,bleacherreport.com,cbssports.com,sports.yahoo.com",
        "language": "en",
        "sortBy": "relevancy",
        "apiKey": api_key,
        "pageSize": 20
    }

    try:
        response = requests.get(url, params=params)

        if response.status_code == 200:
            data = response.json()
            articles = data.get("articles", [])

            if not articles:
                print("No articles found for the given query.")
                return

            print(f"\n--- Fetching Top 20 Sports News: Found {data.get('totalResults')} articles ---\n")

            for i, article in enumerate(articles, 1):
                source = article['source']['name']
                title = article['title']
                article_url = article['url']
                desc = article['description'] if article['description'] else "No description provided."
                author = article.get('author')
                image_url = article.get('urlToImage')
                
                # Create a unique ID based on the URL to prevent duplicates in Firestore
                doc_id = hashlib.md5(article_url.encode()).hexdigest()
                doc_ref = db.collection('sports_news').document(doc_id)
                
                # Scrape full content for a better summary, backup image, and backup author
                scraped_text, backup_image, backup_author = scrape_article_content(article_url)
                content_for_ai = scraped_text if scraped_text and len(scraped_text) > 200 else desc
                
                if not author:
                    author = backup_author
                
                if not image_url:
                    image_url = backup_image

                # Determine category based on keywords in title or content
                category = "Sports"
                search_text = f"{title} {desc} {content_for_ai}".lower()
                for league in ["NBA", "MLB", "NFL"]:
                    if league.lower() in search_text:
                        category = league
                        break

                # Generate AI Summary
                print(f"Processing article {i}: {title}...")
                ai_summary = summarize_article(model, title, content_for_ai)

                # Prepare data for Firestore
                article_data = {
                    "source": source,
                    "title": title,
                    "author": author if author else "Unknown",
                    "url": article_url,
                    "original_description": desc,
                    "image_url": image_url,
                    "ai_summary": ai_summary,
                    "category": category,
                    "timestamp": firestore.SERVER_TIMESTAMP
                }

                # Upload to Firestore
                if db:
                    doc_ref.set(article_data, merge=True)
                    print(f"   Successfully uploaded article to Firestore.")

                # Rate limiting: Ensure less than 5 AI calls per minute
                # Sleep for 13 seconds to stay safely under the limit.
                if i < len(articles):
                    time.sleep(13)

        else:
            print(f"Error: API Request Failed with status {response.status_code}")
    except Exception as e:
        raise Exception(f"Program stopped due to a fatal error during processing: {e}")

if __name__ == "__main__":
    api_key = os.getenv('NEWS_API_KEY')
    db, model = initialize_services()
    if api_key and (db or model):
        get_news_articles(api_key, db, model)
