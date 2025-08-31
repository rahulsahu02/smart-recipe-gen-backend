from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
import os
from dotenv import load_dotenv
from duckduckgo_search import DDGS
from googleapiclient.discovery import build
from google.api_core import exceptions
import json
import base64
from io import BytesIO
from PIL import Image
from flask_pymongo import PyMongo
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required, JWTManager
from werkzeug.security import generate_password_hash, check_password_hash
from bson.objectid import ObjectId

load_dotenv()

# --- CONFIGURATION ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_API_KEY = os.getenv("GOOGLE_SEARCH_API_KEY")
GOOGLE_CSE_ID = os.getenv("CUSTOM_SEARCH_ENGINE_ID")

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    vision_model = genai.GenerativeModel('gemini-2.5-pro')
    text_model = genai.GenerativeModel('gemini-2.5-pro')
else:
    print("WARNING: GOOGLE_API_KEY not set. The application will not work without it.")
    vision_model = None
    text_model = None

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)


# --- DATABASE CONFIGURATION ---
app.config["MONGO_URI"] = os.getenv("MONGO_URI")
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "super-secret")
mongo = PyMongo(app)
jwt = JWTManager(app)

# --- HELPER FUNCTIONS ---
def find_matching_recipes(user_ingredients, dietary_prefs, cuisine, collections):
    print(f"--- Searching in {collections} for recipes with ingredients: {user_ingredients}, dietary: {dietary_prefs}, cuisine: {cuisine} ---")
    
    # Use text search for more flexible matching
    search_query = " ".join(user_ingredients)
    query = {'$text': {'$search': search_query}}

    if cuisine and cuisine.lower() != 'any':
        query['cuisine'] = cuisine
        
    matches = []
    if 'StoredRecipes' in collections:
        matches.extend(list(mongo.db.StoredRecipes.find(query).limit(3)))
    if 'recipes' in collections:
        matches.extend(list(mongo.db.recipes.find(query).limit(3)))
    
    print(f"--- Found {len(matches)} potential matches in the database. ---")
    return matches


def search_web(query):
    results = []
    try:
        print("--- Searching DuckDuckGo... ---")
        with DDGS() as ddgs:
            ddgs_results = list(ddgs.text(query, max_results=5))
            if ddgs_results:
                results.extend([res.get('body', '') for res in ddgs_results])
        print(f"--- Found {len(ddgs_results)} results from DuckDuckGo. ---")
    except Exception as e:
        print(f"--- DuckDuckGo search failed: {e} ---")

    if GOOGLE_CSE_API_KEY and GOOGLE_CSE_ID:
        try:
            print("--- Searching Google... ---")
            service = build("customsearch", "v1", developerKey=GOOGLE_CSE_API_KEY)
            res = service.cse().list(q=query, cx=GOOGLE_CSE_ID, num=5).execute()
            
            if 'items' in res:
                google_results = res['items']
                for item in google_results:
                    results.append(f"{item.get('title', '')}: {item.get('snippet', '')}")
                print(f"--- Found {len(google_results)} results from Google. ---")

        except exceptions.GoogleAPICallError as e:
             print(f"--- Google Search API call failed. Check your API key and CSE ID. Error: {e} ---")
        except Exception as e:
            print(f"--- An unexpected error occurred during Google Search: {e} ---")
    else:
        print("--- Google CSE API Key or ID not provided. Skipping Google Search. ---")
    return results


# --- API ENDPOINTS ---
@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    hashed_password = generate_password_hash(data['password'], method='pbkdf2:sha256')
    mongo.db.users.insert_one({'username': data['username'], 'password': hashed_password, 'favorites': []})
    return jsonify({'message': 'New user created!'})

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    user = mongo.db.users.find_one({'username': data['username']})
    if not user or not check_password_hash(user['password'], data['password']):
        return jsonify({'message': 'Could not verify'}), 401
    access_token = create_access_token(identity=str(user['_id']))
    return jsonify(access_token=access_token)

@app.route('/favorites', methods=['GET'])
@jwt_required()
def get_favorites():
    current_user_id = get_jwt_identity()
    user = mongo.db.users.find_one({'_id': ObjectId(current_user_id)})
    
    favorite_recipes = []
    for fav_id in user.get('favorites', []):
        recipe = mongo.db.recipes.find_one({'_id': ObjectId(fav_id)})
        if not recipe:
            recipe = mongo.db.StoredRecipes.find_one({'_id': ObjectId(fav_id)})
        
        if recipe:
            recipe['_id'] = str(recipe['_id'])
            favorite_recipes.append(recipe)
            
    # Standardize the data before sending to frontend
    formatted_favorites = []
    for recipe in favorite_recipes:
        formatted_favorites.append({
            "id": recipe['_id'],
            "title": recipe.get('title'),
            "description": recipe.get('description'),
            "ingredients": recipe.get('ingredients'),
            "instructions": recipe.get('steps') or recipe.get('instructions'), # Use 'steps' or 'instructions'
            "cookingTime": recipe.get('cooking_time'),
            "difficulty": recipe.get('difficulty'),
            "nutritionalInfo": f"Calories: {recipe.get('nutrition', {}).get('calories', 'N/A')}, Protein: {recipe.get('nutrition', {}).get('protein', 'N/A')}g",
            "servings": f"Serves {recipe.get('servings')}",
        })

    return jsonify(formatted_favorites)

@app.route('/favorite', methods=['POST'])
@jwt_required()
def add_favorite():
    current_user_id = get_jwt_identity()
    recipe_data = request.get_json()
    
    if not recipe_data:
        return jsonify({"message": "No recipe data provided"}), 400
        
    recipe_id = recipe_data.get('id')
    
    if not ObjectId.is_valid(recipe_id):
        new_recipe = {key: recipe_data[key] for key in recipe_data if key != 'id'}
        # Ensure consistency in the saved data
        if 'instructions' in new_recipe:
            new_recipe['steps'] = new_recipe.pop('instructions')
        result = mongo.db.recipes.insert_one(new_recipe)
        recipe_id = str(result.inserted_id)
    
    mongo.db.users.update_one(
        {'_id': ObjectId(current_user_id)},
        {'$addToSet': {'favorites': recipe_id}}
    )
    return jsonify({'message': 'Recipe added to favorites', 'recipe_id': recipe_id})


@app.route('/unfavorite', methods=['POST'])
@jwt_required()
def remove_favorite():
    current_user_id = get_jwt_identity()
    data = request.get_json()
    if not data or 'recipe_id' not in data:
        return jsonify({"message": "Missing recipe_id"}), 400
    
    recipe_id = data['recipe_id']
    
    mongo.db.users.update_one(
        {'_id': ObjectId(current_user_id)},
        {'$pull': {'favorites': recipe_id}}
    )
    
    other_users = mongo.db.users.find_one({'favorites': recipe_id})
    
    if not other_users:
        mongo.db.recipes.delete_one({'_id': ObjectId(recipe_id)})
        print(f"Deleted recipe {recipe_id} as it is no longer favorited by any user.")

    return jsonify({'message': 'Recipe removed from favorites'})


@app.route('/recognize_ingredients', methods=['POST'])
def recognize_ingredients():
    if not vision_model:
        return jsonify({'error': 'Gemini API key not configured on the server.'}), 500
        
    data = request.get_json()
    if not data or 'image' not in data:
        return jsonify({'error': 'No image data provided.'}), 400

    try:
        print("\n\n--- Received image recognition request. ---")
        base64_image_data = data['image'].split(',')[1]
        image_bytes = base64.b64decode(base64_image_data)
        image = Image.open(BytesIO(image_bytes))

        prompt = "Analyze the image and identify all food ingredients. Return them as a simple comma-separated list. Example: tomatoes, onions, chicken breast."
        
        print("--- Sending image to Gemini for recognition... ---")
        response = vision_model.generate_content([prompt, image])
        
        recognized_text = response.text.strip().lower()
        print(f"--- Gemini recognition result: '{recognized_text}' ---")
        
        if not recognized_text:
            return jsonify([])

        ingredients_list = [ing.strip() for ing in recognized_text.split(',') if ing.strip()]
        print(f"--- Parsed ingredients: {ingredients_list} ---")
        
        return jsonify(ingredients_list)

    except Exception as e:
        print(f"--- Error during image recognition: {e} ---")
        return jsonify({'error': 'Failed to process the image.'}), 500


@app.route('/public_recipes', methods=['POST'])
def public_recipes():
    data = request.get_json()
    ingredients = data.get('ingredients', [])
    dietary = data.get('dietary', [])
    servings = data.get('servings', 2)
    cuisine = data.get('cuisine', 'any')
    
    db_matches = find_matching_recipes(ingredients, dietary, cuisine, ['StoredRecipes'])
    
    for recipe in db_matches:
        recipe['id'] = str(recipe.pop('_id'))

    return jsonify(db_matches)


@app.route('/generate_recipes', methods=['POST'])
@jwt_required()
def generate_recipes():
    data = request.get_json()
    if not data or 'ingredients' not in data:
        return jsonify({'error': 'Missing ingredients'}), 400

    ingredients = data.get('ingredients', [])
    dietary = data.get('dietary', [])
    servings = data.get('servings', 2)
    cuisine = data.get('cuisine', 'any')
    print(f"\n\n--- Received recipe request with ingredients: {ingredients}, dietary: {dietary}, servings: {servings}, cuisine: {cuisine} ---")
    
    if not text_model:
        return jsonify({'error': 'Gemini API key not configured on the server.'}), 500
    
    final_recipes = []
    
    # 1. Find and format database matches
    db_matches = find_matching_recipes(ingredients, dietary, cuisine, ['StoredRecipes', 'recipes'])
    if db_matches:
        print(f"--- Found {len(db_matches)} matches in database. Formatting them. ---")
        for recipe in db_matches:
            final_recipes.append({
                "id": str(recipe['_id']),
                "title": recipe['title'],
                "description": recipe.get('description'),
                "ingredients": recipe.get('ingredients'),
                "instructions": recipe.get('steps') or recipe.get('instructions'), # Standardize to 'instructions'
                "cookingTime": recipe.get('cooking_time'),
                "difficulty": recipe.get('difficulty'),
                "nutritionalInfo": f"Calories: {recipe.get('nutrition', {}).get('calories', 'N/A')}, Protein: {recipe.get('nutrition', {}).get('protein', 'N/A')}g",
                "servings": f"Serves {recipe.get('servings')}",
            })

    # 2. Always generate new recipes with AI, using context if available
    prompt = ""
    db_matches_string = json.dumps([r['title'] for r in db_matches])
    
    prompt = f"""
    You are a creative recipe assistant. A user wants to cook with: {', '.join(ingredients)}.
    They want a {cuisine} style recipe for {servings} people, with dietary preferences: {', '.join(dietary) if dietary else 'None'}.
    I have already found these recipes in my database:
    --- DATABASE RECIPES ---
    {db_matches_string}
    --- END DATABASE RECIPES ---
    Please generate 6-7 NEW and DIFFERENT creative recipes that also fit the user's request. Do NOT repeat the recipes I provided above.
    For each new recipe, provide: "title", "description", "ingredients" (list of objects with "name" and "quantity"), "instructions" (list), "cookingTime" (integer), "difficulty", "nutritionalInfo", "servings".
    Format the final output as a valid JSON array of recipe objects. Do not include markdown.
    """

    try:
        print("--- Generating additional recipes with Gemini... ---")
        response = text_model.generate_content(prompt)
        raw_response = response.text.strip()
        
        clean_response = raw_response.replace("```json", "").replace("```", "").strip()
        print(f"--- Cleaned Gemini Response ---\n{clean_response}\n--------------------------")
        
        ai_recipes = json.loads(clean_response)
        
        existing_titles = {recipe['title'].lower() for recipe in final_recipes}
        for recipe in ai_recipes:
            if recipe['title'].lower() not in existing_titles:
                final_recipes.append(recipe)
                existing_titles.add(recipe['title'].lower())

    except Exception as e:
        print(f"--- Error generating or parsing AI recipes: {e} ---")
        if not final_recipes:
            return jsonify({'error': 'Failed to generate recipes.'}), 500

    print(f"--- Returning a total of {len(final_recipes)} combined recipes. ---")
    return jsonify(final_recipes)

# --- RUN THE APP ---
if __name__ == '__main__':
    app.run(debug=True, port=5001)