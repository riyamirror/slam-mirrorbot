import os
import io
import pickle
import urllib.parse as urlparse
from urllib.parse import parse_qs

import re
import json
import requests
import logging
from random import randrange

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from tenacity import *

from telegram import InlineKeyboardMarkup
from bot.helper.telegram_helper import button_build
from telegraph import Telegraph
from bot import parent_id, DOWNLOAD_DIR, IS_TEAM_DRIVE, INDEX_URL, \
    USE_SERVICE_ACCOUNTS, download_dict, telegraph_token, BUTTON_FOUR_NAME, BUTTON_FOUR_URL, BUTTON_FIVE_NAME, BUTTON_FIVE_URL, BUTTON_SIX_NAME, BUTTON_SIX_URL, SHORTENER, SHORTENER_API, IMAGE_URL, VIEW_LINK
from bot.helper.ext_utils.bot_utils import *
from bot.helper.ext_utils.fs_utils import get_mime_type, get_path_size

LOGGER = logging.getLogger(__name__)
logging.getLogger('googleapiclient.discovery').setLevel(logging.ERROR)
if USE_SERVICE_ACCOUNTS:
    SERVICE_ACCOUNT_INDEX = randrange(len(os.listdir("accounts")))
TELEGRAPHLIMIT = 80

class GoogleDriveHelper:
    def __init__(self, name=None, listener=None):
        self.__G_DRIVE_TOKEN_FILE = "token.pickle"
        # Check https://developers.google.com/drive/scopes for all available scopes
        self.__OAUTH_SCOPE = ['https://www.googleapis.com/auth/drive']
        # Redirect URI for installed apps, can be left as is
        self.__REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
        self.__G_DRIVE_DIR_MIME_TYPE = "application/vnd.google-apps.folder"
        self.__G_DRIVE_BASE_DOWNLOAD_URL = "https://drive.google.com/uc?id={}&export=download"
        self.__G_DRIVE_DIR_BASE_DOWNLOAD_URL = "https://drive.google.com/drive/folders/{}"
        self.__listener = listener
        self.__service = self.authorize()
        self.__listener = listener
        self._file_uploaded_bytes = 0
        self._file_downloaded_bytes = 0
        self.uploaded_bytes = 0
        self.downloaded_bytes = 0
        self.UPDATE_INTERVAL = 5
        self.start_time = 0
        self.total_time = 0
        self.dtotal_time = 0
        self._should_update = True
        self.is_uploading = True
        self.is_cancelled = False
        self.status = None
        self.dstatus = None
        self.updater = None
        self.name = name
        self.update_interval = 3
        self.telegraph_content = []
        self.path = []
        self.total_bytes = 0
        self.total_files = 0
        self.total_folders = 0
        self.sa_count = 0

    def cancel(self):
        self.is_cancelled = True
        self.is_uploading = False

    def speed(self):
        """
        It calculates the average upload speed and returns it in bytes/seconds unit
        :return: Upload speed in bytes/second
        """
        try:
            return self.uploaded_bytes / self.total_time
        except ZeroDivisionError:
            return 0

    def dspeed(self):
        try:
            return self.downloaded_bytes / self.dtotal_time
        except ZeroDivisionError:
            return 0   

    @staticmethod
    def getIdFromUrl(link: str):
        if "folders" in link or "file" in link:
            regex = r"https://drive\.google\.com/(drive)?/?u?/?\d?/?(mobile)?/?(file)?(folders)?/?d?/([-\w]+)[?+]?/?(w+)?"
            res = re.search(regex,link)
            if res is None:
                raise IndexError("GDrive ID not found.")
            return res.group(5)
        parsed = urlparse.urlparse(link)
        return parse_qs(parsed.query)['id'][0]

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def _on_upload_progress(self):
        if self.status is not None:
            chunk_size = self.status.total_size * self.status.progress() - self._file_uploaded_bytes
            self._file_uploaded_bytes = self.status.total_size * self.status.progress()
            LOGGER.debug(f'Uploading {self.name}, chunk size: {get_readable_file_size(chunk_size)}')
            self.uploaded_bytes += chunk_size
            self.total_time += self.update_interval

    def __upload_empty_file(self, path, file_name, mime_type, parent_id=None):
        media_body = MediaFileUpload(path,
                                     mimetype=mime_type,
                                     resumable=False)
        file_metadata = {
            'name': file_name,
            'description': 'Uploaded using Slam Mirror Bot',
            'mimeType': mime_type,
        }
        if parent_id is not None:
            file_metadata['parents'] = [parent_id]
        return self.__service.files().create(supportsTeamDrives=True,
                                             body=file_metadata, media_body=media_body).execute()
    def deletefile(self, link: str):
        try:
            file_id = self.getIdFromUrl(link)
        except (KeyError,IndexError):
            msg = "Google Drive ID could not be found in the provided link"
            return msg
        msg = ''
        try:
            res = self.__service.files().delete(fileId=file_id, supportsTeamDrives=IS_TEAM_DRIVE).execute()
            msg = "Successfully deleted"
        except HttpError as err:
            LOGGER.error(str(err))
            if "File not found" in str(err):
                msg = "No such file exist"
            else:
                msg = "Something went wrong check log"
        finally:
            return msg

    def switchServiceAccount(self):
        global SERVICE_ACCOUNT_INDEX
        service_account_count = len(os.listdir("accounts"))
        if SERVICE_ACCOUNT_INDEX == service_account_count - 1:
            SERVICE_ACCOUNT_INDEX = 0
        self.sa_count += 1
        SERVICE_ACCOUNT_INDEX += 1
        LOGGER.info(f"Switching to {SERVICE_ACCOUNT_INDEX}.json service account")
        self.__service = self.authorize()
        
    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def __set_permission(self, drive_id):
        permissions = {
            'role': 'reader',
            'type': 'anyone',
            'value': None,
            'withLink': True
        }
        return self.__service.permissions().create(supportsTeamDrives=True, fileId=drive_id,
                                                   body=permissions).execute()

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def upload_file(self, file_path, file_name, mime_type, parent_id):
        # File body description
        file_metadata = {
            'name': file_name,
            'description': 'Uploaded by Slam Mirror Bot',
            'mimeType': mime_type,
        }
        try:
            self.typee = file_metadata['mimeType']
        except:
            self.typee = 'File' 
        if parent_id is not None:
            file_metadata['parents'] = [parent_id]

        if os.path.getsize(file_path) == 0:
            media_body = MediaFileUpload(file_path,
                                         mimetype=mime_type,
                                         resumable=False)
            response = self.__service.files().create(supportsTeamDrives=True,
                                                     body=file_metadata, media_body=media_body).execute()
            if not IS_TEAM_DRIVE:
                self.__set_permission(response['id'])

            drive_file = self.__service.files().get(supportsTeamDrives=True,
                                                    fileId=response['id']).execute()
            download_url = self.__G_DRIVE_BASE_DOWNLOAD_URL.format(drive_file.get('id'))
            return download_url
        media_body = MediaFileUpload(file_path,
                                     mimetype=mime_type,
                                     resumable=True,
                                     chunksize=50 * 1024 * 1024)

        # Insert a file
        drive_file = self.__service.files().create(supportsTeamDrives=True,
                                                   body=file_metadata, media_body=media_body)
        response = None
        while response is None:
            if self.is_cancelled:
                break
            try:
                self.status, response = drive_file.next_chunk()
            except HttpError as err:
                if err.resp.get('content-type', '').startswith('application/json'):
                    reason = json.loads(err.content).get('error').get('errors')[0].get('reason')
                    if reason == 'userRateLimitExceeded' or reason == 'dailyLimitExceeded':
                        if USE_SERVICE_ACCOUNTS:
                            self.switchServiceAccount()
                            LOGGER.info(f"Got: {reason}, Trying Again.")
                            return self.upload_file(file_path, file_name, mime_type, parent_id)
                        else:
                            self.is_cancelled = True
                            raise err
                    else:
                        raise err
        self._file_uploaded_bytes = 0
        # Insert new permissions
        if not IS_TEAM_DRIVE:
            self.__set_permission(response['id'])
        # Define file instance and get url for download
        drive_file = self.__service.files().get(supportsTeamDrives=True, fileId=response['id']).execute()
        download_url = self.__G_DRIVE_BASE_DOWNLOAD_URL.format(drive_file.get('id'))
        return download_url

    def upload(self, file_name: str):
        if USE_SERVICE_ACCOUNTS:
            self.service_account_count = len(os.listdir("accounts"))
        self.__listener.onUploadStarted()
        file_dir = f"{DOWNLOAD_DIR}{self.__listener.message.message_id}"
        file_path = f"{file_dir}/{file_name}"
        size = get_readable_file_size(get_path_size(file_path))
        LOGGER.info("Uploading File: " + file_path)
        self.start_time = time.time()
        self.updater = setInterval(self.update_interval, self._on_upload_progress)
        if os.path.isfile(file_path):
            try:
                mime_type = get_mime_type(file_path)
                link = self.upload_file(file_path, file_name, mime_type, parent_id)
                if link is None:
                    raise Exception('Upload has been manually cancelled')
                LOGGER.info("Uploaded To G-Drive: " + file_path)
            except Exception as e:
                if isinstance(e, RetryError):
                    LOGGER.info(f"Total Attempts: {e.last_attempt.attempt_number}")
                    err = e.last_attempt.exception()
                else:
                    err = e
                LOGGER.error(err)
                self.__listener.onUploadError(str(err))
                return
            finally:
                self.updater.cancel()
        else:
            try:
                dir_id = self.create_directory(os.path.basename(os.path.abspath(file_name)), parent_id)
                result = self.upload_dir(file_path, dir_id)
                if result is None:
                    raise Exception('Upload has been manually cancelled!')
                LOGGER.info("Uploaded To G-Drive: " + file_name)
                link = f"https://drive.google.com/folderview?id={dir_id}"
            except Exception as e:
                if isinstance(e, RetryError):
                    LOGGER.info(f"Total Attempts: {e.last_attempt.attempt_number}")
                    err = e.last_attempt.exception()
                else:
                    err = e
                LOGGER.error(err)
                self.__listener.onUploadError(str(err))
                return
            finally:
                self.updater.cancel()
        LOGGER.info(download_dict)
        files = self.total_files
        folders = self.total_folders
        typ = self.typee
        self.__listener.onUploadComplete(link, size, files, folders, typ)
        LOGGER.info("Deleting downloaded file/folder..")
        return link

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def copyFile(self, file_id, dest_id):
        body = {
            'parents': [dest_id]
        }

        try:
            res = self.__service.files().copy(supportsAllDrives=True,fileId=file_id,body=body).execute()
            return res
        except HttpError as err:
            if err.resp.get('content-type', '').startswith('application/json'):
                reason = json.loads(err.content).get('error').get('errors')[0].get('reason')
                if reason == 'userRateLimitExceeded' or reason == 'dailyLimitExceeded':
                    if USE_SERVICE_ACCOUNTS:
                        if self.sa_count > self.service_account_count:
                            self.is_cancelled = True
                            raise err
                        else:
                            self.switchServiceAccount()
                            return self.copyFile(file_id,dest_id)
                    else:
                        self.is_cancelled = True
                        raise err
                else:
                    raise err

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def getFileMetadata(self,file_id):
        return self.__service.files().get(supportsAllDrives=True, fileId=file_id,
                                              fields="name,id,mimeType,size").execute()

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def getFilesByFolderId(self,folder_id):
        page_token = None
        q = f"'{folder_id}' in parents"
        files = []
        while True:
            response = self.__service.files().list(supportsTeamDrives=True,
                                                   includeTeamDriveItems=True,
                                                   q=q,
                                                   spaces='drive',
                                                   pageSize=200,
                                                   fields='nextPageToken, files(id, name, mimeType,size)', corpora='allDrives', orderBy='folder, name',
                                                   pageToken=page_token).execute()
            for file in response.get('files', []):
                files.append(file)
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
        return files

    def clone(self, link):
        self.transferred_size = 0
        self.total_files = 0
        self.total_folders = 0
        if USE_SERVICE_ACCOUNTS:
            self.service_account_count = len(os.listdir("accounts"))
        try:
            file_id = self.getIdFromUrl(link)
        except (KeyError,IndexError):
            msg = "Google drive ID could not be found in the provided link"
            return msg
        msg = ""
        LOGGER.info(f"File ID: {file_id}")
        try:
            meta = self.getFileMetadata(file_id)
            if meta.get("mimeType") == self.__G_DRIVE_DIR_MIME_TYPE:
                dir_id = self.create_directory(meta.get('name'), parent_id)
                self.cloneFolder(meta.get('name'), meta.get('name'), meta.get('id'), dir_id)
                msg += f'<b>Filename: </b><code>{meta.get("name")}</code>\n<b>Size: </b><code>{get_readable_file_size(self.transferred_size)}</code>'
                msg += f'\n<b>Type: </b><code>Folder</code>'
                msg += f'\n<b>SubFolders: </b><code>{self.total_folders}</code>'
                msg += f'\n<b>Files: </b><code>{self.total_files}</code>'
                durl = self.__G_DRIVE_DIR_BASE_DOWNLOAD_URL.format(dir_id)
                buttons = button_build.ButtonMaker()
                if SHORTENER is not None and SHORTENER_API is not None:
                    surl = requests.get(f'https://{SHORTENER}/api?api={SHORTENER_API}&url={durl}&format=text').text
                    buttons.buildbutton("☁️ Drive Link", surl)
                else:
                    buttons.buildbutton("☁️ Drive Link", durl)
                if INDEX_URL is not None:
                    url_path = requests.utils.quote(f'{meta.get("name")}')
                    url = f'{INDEX_URL}/{url_path}/'
                    if SHORTENER is not None and SHORTENER_API is not None:
                        siurl = requests.get(f'https://{SHORTENER}/api?api={SHORTENER_API}&url={url}&format=text').text
                        buttons.buildbutton("⚡ Index Link", siurl)
                    else:
                        buttons.buildbutton("⚡ Index Link", url)
                if BUTTON_FOUR_NAME is not None and BUTTON_FOUR_URL is not None:
                    buttons.buildbutton(f"{BUTTON_FOUR_NAME}", f"{BUTTON_FOUR_URL}")
                if BUTTON_FIVE_NAME is not None and BUTTON_FIVE_URL is not None:
                    buttons.buildbutton(f"{BUTTON_FIVE_NAME}", f"{BUTTON_FIVE_URL}")
                if BUTTON_SIX_NAME is not None and BUTTON_SIX_URL is not None:
                    buttons.buildbutton(f"{BUTTON_SIX_NAME}", f"{BUTTON_SIX_URL}")
            else:
                file = self.copyFile(meta.get('id'), parent_id)
                msg += f'<b>Filename: </b><code>{file.get("name")}</code>'
                durl = self.__G_DRIVE_BASE_DOWNLOAD_URL.format(file.get("id"))
                buttons = button_build.ButtonMaker()
                if SHORTENER is not None and SHORTENER_API is not None:
                    surl = requests.get(f'https://{SHORTENER}/api?api={SHORTENER_API}&url={durl}&format=text').text
                    buttons.buildbutton("☁️ Drive Link", surl)
                else:
                    buttons.buildbutton("☁️ Drive Link", durl)
                try:
                    typeee = file.get('mimeType')
                except:
                    typeee = 'File' 
                try:
                    msg += f'\n<b>Size: </b><code>{get_readable_file_size(int(meta.get("size")))}</code>'
                    msg += f'\n<b>Type: </b><code>{typeee}</code>'
                except TypeError:
                    pass
                if INDEX_URL is not None:
                    url_path = requests.utils.quote(f'{file.get("name")}')
                    url = f'{INDEX_URL}/{url_path}'
                    urls = f'{INDEX_URL}/{url_path}?a=view'
                    if SHORTENER is not None and SHORTENER_API is not None:
                        siurl = requests.get(f'https://{SHORTENER}/api?api={SHORTENER_API}&url={url}&format=text').text
                        siurls = requests.get(f'https://{SHORTENER}/api?api={SHORTENER_API}&url={urls}&format=text').text
                        buttons.buildbutton("⚡ Index Link", siurl)
                        if VIEW_LINK:
                            buttons.buildbutton("🌐 View Link", siurls)
                    else:
                        buttons.buildbutton("⚡ Index Link", url)
                        if VIEW_LINK:
                            buttons.buildbutton("🌐 View Link", urls)
                if BUTTON_FOUR_NAME is not None and BUTTON_FOUR_URL is not None:
                    buttons.buildbutton(f"{BUTTON_FOUR_NAME}", f"{BUTTON_FOUR_URL}")
                if BUTTON_FIVE_NAME is not None and BUTTON_FIVE_URL is not None:
                    buttons.buildbutton(f"{BUTTON_FIVE_NAME}", f"{BUTTON_FIVE_URL}")
                if BUTTON_SIX_NAME is not None and BUTTON_SIX_URL is not None:
                    buttons.buildbutton(f"{BUTTON_SIX_NAME}", f"{BUTTON_SIX_URL}")
        except Exception as err:
            if isinstance(err, RetryError):
                LOGGER.info(f"Total Attempts: {err.last_attempt.attempt_number}")
                err = err.last_attempt.exception()
            err = str(err).replace('>', '').replace('<', '')
            LOGGER.error(err)
            if "User rate limit exceeded" in str(err):
                msg = "User rate limit exceeded."
            elif "File not found" in str(err):
                msg = "File not found."
            else:
                msg = f"Error.\n{err}"
            return msg, ""
        return msg, InlineKeyboardMarkup(buttons.build_menu(2))

    def cloneFolder(self, name, local_path, folder_id, parent_id):
        LOGGER.info(f"Syncing: {local_path}")
        files = self.getFilesByFolderId(folder_id)
        new_id = None
        if len(files) == 0:
            return parent_id
        for file in files:
            if file.get('mimeType') == self.__G_DRIVE_DIR_MIME_TYPE:
                self.total_folders += 1
                file_path = os.path.join(local_path, file.get('name'))
                current_dir_id = self.create_directory(file.get('name'), parent_id)
                new_id = self.cloneFolder(file.get('name'), file_path, file.get('id'), current_dir_id)
            else:
                try:
                    self.total_files += 1
                    self.transferred_size += int(file.get('size'))
                except TypeError:
                    pass
                self.copyFile(file.get('id'), parent_id)
                new_id = parent_id
            if self.is_cancelled:
                break

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def create_directory(self, directory_name, parent_id):
        file_metadata = {
            "name": directory_name,
            "mimeType": self.__G_DRIVE_DIR_MIME_TYPE
        }
        if parent_id is not None:
            file_metadata["parents"] = [parent_id]
        file = self.__service.files().create(supportsTeamDrives=True, body=file_metadata).execute()
        file_id = file.get("id")
        if not IS_TEAM_DRIVE:
            self.__set_permission(file_id)
        LOGGER.info("Created Google-Drive Folder:\nName: {}\nID: {} ".format(file.get("name"), file_id))
        return file_id

    def upload_dir(self, input_directory, parent_id):
        list_dirs = os.listdir(input_directory)
        if len(list_dirs) == 0:
            return parent_id
        new_id = None
        for item in list_dirs:
            current_file_name = os.path.join(input_directory, item)
            if os.path.isdir(current_file_name):
                current_dir_id = self.create_directory(item, parent_id)
                new_id = self.upload_dir(current_file_name, current_dir_id)
                self.total_folders += 1
            else:
                mime_type = get_mime_type(current_file_name)
                file_name = current_file_name.split("/")[-1]
                # current_file_name will have the full path
                self.upload_file(current_file_name, file_name, mime_type, parent_id)
                self.total_files += 1
                new_id = parent_id
            if self.is_cancelled:
                break
        return new_id

    def authorize(self):
        # Get credentials
        credentials = None
        if not USE_SERVICE_ACCOUNTS:
            if os.path.exists(self.__G_DRIVE_TOKEN_FILE):
                with open(self.__G_DRIVE_TOKEN_FILE, 'rb') as f:
                    credentials = pickle.load(f)
            if credentials is None or not credentials.valid:
                if credentials and credentials.expired and credentials.refresh_token:
                    credentials.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        'credentials.json', self.__OAUTH_SCOPE)
                    LOGGER.info(flow)
                    credentials = flow.run_console(port=0)

                # Save the credentials for the next run
                with open(self.__G_DRIVE_TOKEN_FILE, 'wb') as token:
                    pickle.dump(credentials, token)
        else:
            LOGGER.info(f"Authorizing with {SERVICE_ACCOUNT_INDEX}.json service account")
            credentials = service_account.Credentials.from_service_account_file(
                f'accounts/{SERVICE_ACCOUNT_INDEX}.json',
                scopes=self.__OAUTH_SCOPE)
        return build('drive', 'v3', credentials=credentials, cache_discovery=False)
    def edit_telegraph(self):
        nxt_page = 1 
        prev_page = 0
        for content in self.telegraph_content :
            if nxt_page == 1 :
                content += f'<b><a href="https://telegra.ph/{self.path[nxt_page]}">Next</a></b>'
                nxt_page += 1
            else :
                if prev_page <= self.num_of_path:
                    content += f'<b><a href="https://telegra.ph/{self.path[prev_page]}">Prev</a></b>'
                    prev_page += 1
                if nxt_page < self.num_of_path:
                    content += f'<b> | <a href="https://telegra.ph/{self.path[nxt_page]}">Next</a></b>'
                    nxt_page += 1
            Telegraph(access_token=telegraph_token).edit_page(path = self.path[prev_page],
                                 title = 'Onlyfans4uu Search',
                                 author_name='Onlyfans4uu',
                                 author_url='onlyfans4uu',
                                 html_content=content)
        return

    def escapes(self, str):	
        chars = ['\\', "'", '"', r'\a', r'\b', r'\f', r'\n', r'\r', r'\t']	
        for char in chars:	
            str = str.replace(char, '\\'+char)	
        return str	

    def drive_list(self, fileName):
        msg = ""
        fileName = self.escapes(str(fileName))
        # Create Search Query for API request.
        query = f"'{parent_id}' in parents and (name contains '{fileName}')"
        response = self.__service.files().list(supportsTeamDrives=True,
                                               includeTeamDriveItems=True,
                                               q=query,
                                               spaces='drive',
                                               pageSize=200,
                                               fields='files(id, name, mimeType, size)',
                                               orderBy='name asc').execute()
        content_count = 0
        if response["files"]:
            msg += f'<img src="{IMAGE_URL}" /><h4>{len(response["files"])} Results: {fileName}</h4><br><br>'
            for file in response.get('files', []):
                if file.get('mimeType') == "application/vnd.google-apps.folder":  # Detect Whether Current Entity is a Folder or File.
                    furl = f"https://drive.google.com/drive/folders/{file.get('id')}"
                    msg += f"📁 <code>{file.get('name')}<br>(folder)</code><br>"
                    if SHORTENER is not None and SHORTENER_API is not None:
                        sfurl = requests.get(f'https://{SHORTENER}/api?api={SHORTENER_API}&url={furl}&format=text').text
                        msg += f"<b><a href={sfurl}>Drive Link</a></b>"
                    else:
                        msg += f"<b><a href={furl}>Drive Link</a></b>"
                    if INDEX_URL is not None:
                        url_path = requests.utils.quote(f'{file.get("name")}')
                        url = f'{INDEX_URL}/{url_path}/'
                        if SHORTENER is not None and SHORTENER_API is not None:
                            siurl = requests.get(f'https://{SHORTENER}/api?api={SHORTENER_API}&url={url}&format=text').text
                            msg += f' <b>| <a href="{siurl}">Index Link</a></b>'
                        else:
                            msg += f' <b>| <a href="{url}">Index Link</a></b>'
                elif file.get('mimeType') == 'application/vnd.google-apps.shortcut':
                    msg += f"⁍<a href='https://drive.google.com/drive/folders/{file.get('id')}'>{file.get('name')}" \
                        f"</a> (shortcut)"
                    # Excluded index link as indexes cant download or open these shortcuts
                else:
                    furl = f"https://drive.google.com/uc?id={file.get('id')}&export=download"
                    msg += f"📄 <code>{file.get('name')}<br>({get_readable_file_size(int(file.get('size')))})</code><br>"
                    if SHORTENER is not None and SHORTENER_API is not None:
                        sfurl = requests.get(f'https://{SHORTENER}/api?api={SHORTENER_API}&url={furl}&format=text').text
                        msg += f"<b><a href={sfurl}>Drive Link</a></b>"
                    else:
                        msg += f"<b><a href={furl}>Drive Link</a></b>"
                    if INDEX_URL is not None:
                        url_path = requests.utils.quote(f'{file.get("name")}')
                        url = f'{INDEX_URL}/{url_path}'
                        urls = f'{INDEX_URL}/{url_path}?a=view'
                        if SHORTENER is not None and SHORTENER_API is not None:
                            siurl = requests.get(f'https://{SHORTENER}/api?api={SHORTENER_API}&url={url}&format=text').text
                            siurls = requests.get(f'https://{SHORTENER}/api?api={SHORTENER_API}&url={urls}&format=text').text
                            msg += f' <b>| <a href="{siurl}">Index Link</a></b>'
                            if VIEW_LINK:
                                msg += f' <b>| <a href="{siurls}">View Link</a></b>'
                        else:
                            msg += f' <b>| <a href="{url}">Index Link</a></b>'
                            if VIEW_LINK:
                                msg += f' <b>| <a href="{urls}">View Link</a></b>'
                msg += '<br><br>'
                content_count += 1
                if content_count == TELEGRAPHLIMIT :
                    self.telegraph_content.append(msg)
                    msg = ""
                    content_count = 0
                    
            if msg != '':
                self.telegraph_content.append(msg)

            if len(self.telegraph_content) == 0:
                return "No Result Found ❌", None

            for content in self.telegraph_content :
                self.path.append(Telegraph(access_token=telegraph_token).create_page(
                                                        title = 'Onlyfans4uu Search',
                                                        author_name='Onlyfans4uu',
                                                        author_url='Onlyfans4uu',
                                                        html_content=content
                                                        )['path'])

            self.num_of_path = len(self.path)
            if self.num_of_path > 1:
                self.edit_telegraph()

            msg = f"<b>Found {len(response['files'])} results for <i>{fileName}</i></b>"
            buttons = button_build.ButtonMaker()   
            buttons.buildbutton("🔎 VIEW", f"https://telegra.ph/{self.path[0]}")

            return msg, InlineKeyboardMarkup(buttons.build_menu(1))

        else :
            return '', ''

    def count(self, link):
        try:
            file_id = self.getIdFromUrl(link)
        except (KeyError,IndexError):
            msg = "Google drive ID could not be found in the provided link"
            return msg
        msg = ""
        LOGGER.info(f"File ID: {file_id}")
        try:
            drive_file = self.__service.files().get(fileId=file_id, fields="id, name, mimeType, size",
                                                   supportsTeamDrives=True).execute()
            name = drive_file['name']
            LOGGER.info(f"Counting: {name}")
            if drive_file['mimeType'] == self.__G_DRIVE_DIR_MIME_TYPE:
                self.gDrive_directory(**drive_file)
                msg += f'<b>Filename: </b><code>{name}</code>'
                msg += f'\n<b>Size: </b><code>{get_readable_file_size(self.total_bytes)}</code>'
                msg += f'\n<b>Type: </b><code>Folder</code>'
                msg += f'\n<b>SubFolders: </b><code>{self.total_folders}</code>'
                msg += f'\n<b>Files: </b><code>{self.total_files}</code>'
            else:
                msg += f'<b>Filename: </b><code>{name}</code>'
                try:
                    typee = drive_file['mimeType']
                except:
                    typee = 'File'    
                try:
                    self.total_files += 1
                    self.gDrive_file(**drive_file)
                    msg += f'\n<b>Size: </b><code>{get_readable_file_size(self.total_bytes)}</code>'
                    msg += f'\n<b>Type: </b><code>{typee}</code>'
                    msg += f'\n<b>Files: </b><code>{self.total_files}</code>'
                except TypeError:
                    pass
        except Exception as err:
            err = str(err).replace('>', '').replace('<', '')
            LOGGER.error(err)
            if "File not found" in str(err):
                msg = "File not found."
            else:
                msg = f"Error.\n{err}"
            return msg
        return msg

    def gDrive_file(self, **kwargs):
        try:
            size = int(kwargs['size'])
        except:
            size = 0
        self.total_bytes += size

    def gDrive_directory(self, **kwargs) -> None:
        files = self.getFilesByFolderId(kwargs['id'])
        if len(files) == 0:
            return
        for file_ in files:
            if file_['mimeType'] == self.__G_DRIVE_DIR_MIME_TYPE:
                self.total_folders += 1
                self.gDrive_directory(**file_)
            else:
                self.total_files += 1
                self.gDrive_file(**file_)

    def clonehelper(self, link):
        try:
            file_id = self.getIdFromUrl(link)
        except (KeyError,IndexError):
            msg = "Google drive ID could not be found in the provided link"
            return msg, "", ""
        LOGGER.info(f"File ID: {file_id}")
        try:
            drive_file = self.__service.files().get(fileId=file_id, fields="id, name, mimeType, size",
                                                   supportsTeamDrives=True).execute()
            name = drive_file['name']
            LOGGER.info(f"Checking: {name}")
            if drive_file['mimeType'] == self.__G_DRIVE_DIR_MIME_TYPE:
                self.gDrive_directory(**drive_file)
            else:
                try:
                    self.total_files += 1
                    self.gDrive_file(**drive_file)
                except TypeError:
                    pass
            clonesize = self.total_bytes
        except Exception as err:
            err = str(err).replace('>', '').replace('<', '')
            LOGGER.error(err)
            if "File not found" in str(err):
                msg = "File not found."
            else:
                msg = f"Error.\n{err}"
            return msg, "", ""
        return "", clonesize, name

    def download(self, link):
        file_id = self.getIdFromUrl(link)
        if USE_SERVICE_ACCOUNTS:
            self.service_account_count = len(os.listdir("accounts"))
        self.start_time = time.time()
        self.updater = setInterval(self.update_interval, self._on_download_progress)
        try:
            meta = self.getFileMetadata(file_id)
            path = f"{DOWNLOAD_DIR}{self.__listener.uid}/"
            if meta.get("mimeType") == self.__G_DRIVE_DIR_MIME_TYPE:
                self.download_folder(file_id, path, meta.get('name'))
            else:
                os.makedirs(path)
                self.download_file(file_id, path, meta.get('name'), meta.get('mimeType'))
        except Exception as err:
            err = str(err).replace('>', '').replace('<', '')
            LOGGER.error(err)
            if "downloadQuotaExceeded" in str(err):
                err = "Download Quota Exceeded."
            self.__listener.onDownloadError(err)
            return
        finally:
            self.updater.cancel()
            if self.is_cancelled:
                return
        self.__listener.onDownloadComplete()

    def download_folder(self, folder_id, path, folder_name):
        if not os.path.exists(path + folder_name):
            os.makedirs(path + folder_name)
        path += folder_name + '/'
        result = []
        page_token = None
        while True:
            files = self.__service.files().list(
                    supportsTeamDrives=True,
                    includeTeamDriveItems=True,
                    q=f"'{folder_id}' in parents",
                    fields='nextPageToken, files(id, name, mimeType, size, shortcutDetails)',
                    pageToken=page_token,
                    pageSize=1000).execute()
            result.extend(files['files'])
            page_token = files.get("nextPageToken")
            if not page_token:
                break

        result = sorted(result, key=lambda k: k['name'])
        for item in result:
            file_id = item['id']
            filename = item['name']
            mime_type = item['mimeType']
            shortcut_details = item.get('shortcutDetails', None)
            if shortcut_details != None:
                file_id = shortcut_details['targetId']
                mime_type = shortcut_details['targetMimeType']
            if mime_type == 'application/vnd.google-apps.folder':
                self.download_folder(file_id, path, filename)
            elif not os.path.isfile(path + filename):
                self.download_file(file_id, path, filename, mime_type)
            if self.is_cancelled:
                break

    def download_file(self, file_id, path, filename, mime_type):
        request = self.__service.files().get_media(fileId=file_id)
        fh = io.FileIO('{}{}'.format(path, filename), 'wb')
        downloader = MediaIoBaseDownload(fh, request, chunksize = 65 * 1024 * 1024)
        done = False
        while done is False:
            if self.is_cancelled:
                fh.close()
                break
            try:
                self.dstatus, done = downloader.next_chunk()
            except HttpError as err:
                if err.resp.get('content-type', '').startswith('application/json'):
                    reason = json.loads(err.content).get('error').get('errors')[0].get('reason')
                    if reason == 'downloadQuotaExceeded' or reason == 'dailyLimitExceeded':
                        if USE_SERVICE_ACCOUNTS:
                            if self.sa_count > self.service_account_count:
                                self.is_cancelled = True
                                raise err
                            else:
                                self.switchServiceAccount()
                                LOGGER.info(f"Got: {reason}, Trying Again...")
                                return self.download_file(file_id, path, filename, mime_type)
                        else:
                            self.is_cancelled = True
                            raise err
                    else:
                        raise err
        self._file_downloaded_bytes = 0

    def _on_download_progress(self):
        if self.dstatus is not None:
            chunk_size = self.dstatus.total_size * self.dstatus.progress() - self._file_downloaded_bytes
            self._file_downloaded_bytes = self.dstatus.total_size * self.dstatus.progress()
            self.downloaded_bytes += chunk_size
            self.dtotal_time += self.update_interval
    
    def cancel_download(self):
        self.is_cancelled = True
        LOGGER.info(f"Cancelling Download: {self.name}")
        self.__listener.onDownloadError('Download stopped by user!')
