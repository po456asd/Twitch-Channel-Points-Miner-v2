# Based on https://github.com/derrod/twl.py
# Original Copyright (c) 2020 Rodney
# The MIT License (MIT)

import copy
import getpass
import logging
import os
import pickle

import browser_cookie3
import requests

from TwitchChannelPointsMiner.classes.Exceptions import (
    BadCredentialsException,
    WrongCookiesException,
)
from TwitchChannelPointsMiner.constants import GQLOperations

logger = logging.getLogger(__name__)


class TwitchLogin(object):
    __slots__ = [
        "client_id",
        "token",
        "login_check_result",
        "session",
        "session",
        "username",
        "password",
        "user_id",
        "email",
        "cookies",
    ]

    def __init__(self, client_id, username, user_agent, password=None):
        self.client_id = client_id
        self.token = None
        self.login_check_result = False
        self.session = requests.session()
        self.session.headers.update(
            {"Client-ID": self.client_id, "User-Agent": user_agent}
        )
        self.username = username
        self.password = password
        self.user_id = None
        self.email = None

        self.cookies = []

    def login_flow(self):
        logger.info("You'll have to login to Twitch!")

        post_data = {
            "client_id": self.client_id,
            "undelete_user": False,
            "remember_me": True,
        }

        use_backup_flow = False

        for attempt in range(0, 25):
            password = (
                getpass.getpass(f"Enter Twitch password for {self.username}: ")
                if self.password in [None, ""]
                else self.password
            )

            post_data["username"] = self.username
            post_data["password"] = password

            while True:
                # Try login without 2FA
                login_response = self.send_login_request(post_data)

                if "captcha_proof" in login_response:
                    post_data["captcha"] = dict(proof=login_response["captcha_proof"])

                if "error_code" in login_response:
                    err_code = login_response["error_code"]
                    if err_code in [3011, 3012]:  # missing 2fa token
                        if err_code == 3011:
                            logger.info(
                                "Two factor authentication enabled, please enter token below."
                            )
                        else:
                            logger.info("Invalid two factor token, please try again.")

                        twofa = input("2FA token: ")
                        post_data["authy_token"] = twofa.strip()
                        continue

                    elif err_code in [3022, 3023]:  # missing 2fa token
                        if err_code == 3022:
                            logger.info("Login Verification code required.")
                            self.email = login_response["obscured_email"]
                        else:
                            logger.info(
                                "Invalid Login Verification code entered, please try again."
                            )

                        twofa = input(
                            f"Please enter the 6-digit code sent to {self.email}: "
                        )
                        post_data["twitchguard_code"] = twofa.strip()
                        continue

                    # invalid password, or password not provided
                    elif err_code in [3001, 3003]:
                        logger.info("Invalid username or password, please try again.")

                        # If the password is loaded from run.py, require the user to fix it there.
                        if self.password not in [None, ""]:
                            raise BadCredentialsException(
                                "Username or password is incorrect."
                            )

                        # If the user didn't load the password from run.py we can just ask for it again.
                        break
                    elif err_code == 1000:
                        logger.info(
                            "Console login unavailable (CAPTCHA solving required)."
                        )
                        use_backup_flow = True
                        break
                    else:
                        #logger.error(f"Unknown error: {login_response}")
                        #raise NotImplementedError(
                        #    f"Unknown TwitchAPI error code: {err_code}"
                        #)
                        use_backup_flow = True
                        break

                if "access_token" in login_response:
                    self.set_token(login_response["access_token"])
                    return self.check_login()

            if use_backup_flow:
                break

        if use_backup_flow:
            self.set_token(self.login_flow_backup())
            return self.check_login()

        return False

    def set_token(self, new_token):
        self.token = new_token
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def send_login_request(self, json_data):
        response = self.session.post("https://passport.twitch.tv/login", json=json_data)
        return response.json()

    def login_flow_backup(self):
        """Backup OAuth login flow in case manual captcha solving is required"""
        browser = input(
            "What browser do you use? Chrome (1), Firefox (2), Other (3): "
        ).strip()
        if browser not in ("1", "2"):
            logger.info("Your browser is unsupported, sorry.")
            return None

        input(
            "Please login inside your browser of choice (NOT incognito mode) and press Enter..."
        )
        logger.info("Loading cookies saved on your computer...")
        twitch_domain = ".twitch.tv"
        if browser == "1":  # chrome
            cookie_jar = browser_cookie3.chrome(domain_name=twitch_domain)
        else:
            cookie_jar = browser_cookie3.firefox(domain_name=twitch_domain)
        cookies_dict = requests.utils.dict_from_cookiejar(cookie_jar)
        self.username = cookies_dict.get("login")
        return cookies_dict.get("auth-token")

    def check_login(self):
        if self.login_check_result:
            return self.login_check_result
        if self.token is None:
            return False

        self.login_check_result = self.__set_user_id()
        return self.login_check_result

    def save_cookies(self, cookies_file):
        cookies_dict = self.session.cookies.get_dict()
        cookies_dict["auth-token"] = self.token
        if "persistent" not in cookies_dict:  # saving user id cookies
            cookies_dict["persistent"] = self.user_id

        self.cookies = []
        for cookie_name, value in cookies_dict.items():
            self.cookies.append({"name": cookie_name, "value": value})
        pickle.dump(self.cookies, open(cookies_file, "wb"))

    def get_cookie_value(self, key):
        for cookie in self.cookies:
            if cookie["name"] == key:
                if cookie["value"] is not None:
                    return cookie["value"]
        return None

    def load_cookies(self, cookies_file):
        if os.path.isfile(cookies_file):
            self.cookies = pickle.load(open(cookies_file, "rb"))
        else:
            raise WrongCookiesException("There must be a cookies file!")

    def get_user_id(self):
        persistent = self.get_cookie_value("persistent")
        user_id = (
            int(persistent.split("%")[0]) if persistent is not None else self.user_id
        )
        if user_id is None:
            if self.__set_user_id() is True:
                return self.user_id
        return user_id

    def __set_user_id(self):
        json_data = copy.deepcopy(GQLOperations.ReportMenuItem)
        json_data["variables"] = {"channelLogin": self.username}
        response = self.session.post(GQLOperations.url, json=json_data)

        if response.status_code == 200:
            json_response = response.json()
            if (
                "data" in json_response
                and "user" in json_response["data"]
                and json_response["data"]["user"]["id"] is not None
            ):
                self.user_id = json_response["data"]["user"]["id"]
                return True
        return False

    def get_auth_token(self):
        return self.get_cookie_value("auth-token")
