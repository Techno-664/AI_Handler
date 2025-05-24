from fastapi import FastAPI, HTTPException, Path, Request
import secrets
import time
import asyncio
import nodriver as uc
from contextlib import asynccontextmanager
import re
import os


def extract_text_from_html(html_content):
    html_content = str(html_content)
    # Remove script and style elements with their content
    html_content = re.sub(r'<script.*?</script>', '', html_content, flags=re.DOTALL)
    html_content = re.sub(r'<style.*?</style>', '', html_content, flags=re.DOTALL)

    # Remove HTML comments
    html_content = re.sub(r'<!--.*?-->', '', html_content, flags=re.DOTALL)

    # Replace block elements with newlines (common block tags that should create line breaks)
    for tag in ['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'br', 'tr']:
        html_content = re.sub(r'<\s*' + tag + r'[^>]*>', '\n', html_content, flags=re.IGNORECASE)
        html_content = re.sub(r'<\s*/' + tag + r'\s*>', '\n', html_content, flags=re.IGNORECASE)

    # Remove remaining HTML tags
    html_content = re.sub(r'<[^>]*>', ' ', html_content)

    # Clean up whitespace but preserve newlines
    # 1. Replace multiple spaces with a single space
    text = re.sub(r' +', ' ', html_content)
    # 2. Handle multiple newlines (reduce to max 2 consecutive newlines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    # 3. Remove spaces at the beginning and end of each line
    text = re.sub(r'^ +| +$', '', text, flags=re.MULTILINE)
    # 4. Final trim
    text = text.strip()

    return text


# Configuration
SESSION_TIMEOUT = 10 * 60  # 10 minutes in seconds
CLEANUP_INTERVAL = 30  # seconds - how often to check for expired sessions
sessions = {}


# Background cleanup task
async def cleanup_expired_sessions():
    while True:
        now = time.time()
        # Find and remove expired sessions
        for sid, info in list(sessions.items()):
            if now - info['last'] > SESSION_TIMEOUT:
                info['browser'].stop()
                sessions.pop(sid, None)
        await asyncio.sleep(CLEANUP_INTERVAL)


# FastAPI application lifespan management
@asynccontextmanager
async def lifespan(_app: FastAPI):
    cleanup_task = asyncio.create_task(cleanup_expired_sessions())
    yield
    # Cleanup on shutdown
    cleanup_task.cancel()
    # Close any remaining browser sessions
    for info in sessions.values():
        info['browser'].stop()


# Initialize FastAPI app
app = FastAPI(lifespan=lifespan)


@app.post("/sessions")
async def create_session(request: Request):
    data = await request.json()
    username = data.get('username')
    password = data.get('password')

    # Launch browser
    browser = await uc.start()
    page = await browser.get('https://chat.deepseek.com/sign_in')
    await page.select('body', timeout=100000)

    # Find and fill login form
    email_input = await page.select('input[placeholder="Phone number / email address"]')
    await email_input.send_keys(username)

    pwd_input = await page.select('input[placeholder="Password"]')
    await pwd_input.send_keys(password)

    # Submit login form
    login_btn = await page.find_element_by_text('Log in', best_match=True)
    await login_btn.mouse_click()

    # Generate a unique session ID
    sid = secrets.token_urlsafe(16)
    sessions[sid] = {
        'browser': browser,
        'page': page,
        'last': time.time()
    }

    return {"session_id": sid}


@app.post("/sessions/{sid}/logout")
async def logout(sid: str = Path(...)):
    if sid not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    sessions[sid]['browser'].stop()
    sessions.pop(sid)
    return {"status": "logged_out"}


@app.post("/sessions/{sid}/refresh")
async def refresh(sid: str = Path(...)):
    if sid not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    sessions[sid]['last'] = time.time()
    return {"status": "refreshed"}


@app.post("/sessions/{sid}/chat")
async def send_chat(request: Request, sid: str = Path(...)):
    if sid not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    data = await request.json()
    prompt = data.get('prompt')
    files = data.get('files')  # This should be a list of file paths

    # Update last access time
    sessions[sid]['last'] = time.time()
    page = sessions[sid]['page']

    # Handle file uploads if files are provided
    if files:
        try:
            # Find the file input element
            file_input = await page.select("input[type='file']")

            # Convert to list if single file, then loop through all
            file_list = files if isinstance(files, list) else [files]

            for file_path in file_list:
                if os.path.exists(file_path):
                    await file_input.send_file(file_path)
                else:
                    print(f"File not found, skipping: {file_path}")

            print(f"File upload process completed")

        except Exception as e:
            print(f"Error uploading files: {e}")
            raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")

    # Find and clear input field, then send prompt
    input_area = await page.select('#chat-input')
    await input_area.send_keys(prompt)

    # Send message
    while True:
        try:
            send_button = await page.select('div[role="button"][aria-disabled="false"]._7436101', timeout=1000)
            await send_button.mouse_click()
            break
        except:
            pass

    # Wait for response to finish
    await asyncio.sleep(10)
    while True:
        try:
            await page.select('div[role="button"][aria-disabled="true"]._7436101', timeout=1000)
            break
        except:
            pass

    # Get all response elements using the class-based selector
    response_elements = await page.select_all('.ds-markdown.ds-markdown')
    last_response = response_elements[-1]
    response_text = extract_text_from_html(last_response)

    return {
        "response": response_text,
        "status": "success",
        "files_uploaded": files if files else None
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
