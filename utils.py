# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import random
import string
from base64 import urlsafe_b64encode
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple, Union
from typing import TYPE_CHECKING

import google.auth

from viur.core import conf, db, errors

if TYPE_CHECKING:
	from .skeleton import SkeletonInstance

# Proxy to context depended variables
currentRequest = ContextVar("Request", default=None)
currentRequestData = ContextVar("Request-Data", default=None)
currentSession = ContextVar("Session", default=None)
currentLanguage = ContextVar("Language", default=None)

# Determine which ProjectID we currently run in (as the app_identity module isn't available anymore)
_, projectID = google.auth.default()
del _
# Determine our basePath (as os.getCWD is broken on appengine)
projectBasePath = globals()["__file__"].replace("/viur/core/utils.py","")
isLocalDevelopmentServer = os.environ['GAE_ENV'] == "localdev"

def utcNow():
	return datetime.now(timezone.utc)

def generateRandomString(length: int = 13) -> str:
	"""
	Return a string containing random characters of given *length*.
	Its safe to use this string in URLs or HTML.

	:type length: int
	:param length: The desired length of the generated string.

	:returns: A string with random characters of the given length.
	:rtype: str
	"""
	return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def sendEMail(dests: Union[str, List[str]],
			  file: str = None,
			  template: str = None,
			  skel: Union[None, Dict, SkeletonInstance, List[SkeletonInstance]] = None,
			  attachments: List[Tuple[str, str]] = None,
			  sender: str = None,
			  cc: Union[str, List[str]] = None,
			  bcc: Union[str, List[str]] = None,
			  replyTo: str = None,
			  header: Dict = None,
			  template_params: Any = None,
			  *args, **kwargs) -> Any:
	"""
	General purpose function for sending e-mail.

	This function allows for sending e-mails, also with generated content using the Jinja2 template engine.

	Your have to implement a method which should be called to send the prepared email finally. For this you have
	to allocate *viur.emailHandler* in conf.

	:param dests: A list of addresses to send this mail to. A bare string will be treated as a list with 1 address.
	:param file: The name of a template from the deploy/emails directory.
	:param template: This string is interpreted as the template contents. Alternative to load from template file.
	:param skel: The data made available to the template. In case of a Skeleton or SkelList, its parsed the usual way;\
		Dictionaries are passed unchanged.
	:param attachments: List of files ((filename, filecontent)-pairs) to be sent within the mail as attachments.
	:param sender: The address sending this mail.
	:param cc: Carbon-copy recipients. A bare string will be treated as a list with 1 address.
	:param bcc: Blind carbon-copy recipients. A bare string will be treated as a list with 1 address.
	:param replyTo: A reply-to email address.
	:param header: Specify headers for this email.
	:param template_params: Supply params for the template.
	"""

	def normalizeToList(value: Union[None, Any, List[Any]]) -> List[Any]:
		if value is None:
			return []
		if isinstance(value, list):
			return value
		return [value]

	if not (bool(file) ^ bool(template)):
		raise ValueError("You have to set the params 'file' xor a 'template'.")

	dests = normalizeToList(dests)
	cc = normalizeToList(cc)
	bcc = normalizeToList(bcc)
	attachments = normalizeToList(attachments)

	if conf["viur.emailRecipientOverride"]:
		logging.warning("Overriding destination %s with %s", dests, conf["viur.emailRecipientOverride"])

		oldDests = dests

		newDests = normalizeToList(conf["viur.emailRecipientOverride"])

		dests = []
		for newDest in newDests:
			if newDest.startswith("@"):
				for oldDest in oldDests:
					dests.append(oldDest.replace(".", "_dot_").replace("@", "_at_") + newDest)
			else:
				dests.append(newDest)

	elif conf["viur.emailRecipientOverride"] is False:
		logging.warning("Sending emails disabled by config[viur.emailRecipientOverride]")
		return False

	if conf["viur.emailSenderOverride"]:
		sender = conf["viur.emailSenderOverride"]
	elif sender is None and os.getenv("GAE_ENV") == "localdev":
		sender = f"viur@localdev.{projectID}.appspotmail.com"
	elif sender is None:
		sender = f"viur@{os.getenv('GAE_VERSION')}.{projectID}.appspotmail.com"

	handler = conf.get("viur.emailHandler")

	if handler is None:
		raise errors.InvalidConfigException("No emailHandler specified!")
	elif not callable(handler):
		raise errors.InvalidConfigException("Invalid emailHandler configured, no email will be sent!")

	subject, body = conf["viur.emailRenderer"](dests, file, template, skel, template_params, **kwargs)

	return handler(dests=dests, sender=sender, cc=cc, bcc=bcc, replyTo=replyTo,
				   subject=subject, header=header, body=body, attachments=attachments, *args, **kwargs)


def sendEMailToAdmins(subject: str, body: str, *args, **kwargs):
	"""
		Sends an e-mail to the root users of the current app.

		:param subject: Defines the subject of the message.
		:param body: Defines the message body.
	"""
	success = False
	try:
		if "user" in dir(conf["viur.mainApp"]):
			users = []
			for userSkel in conf["viur.mainApp"].user.viewSkel().all().filter("access =", "root").fetch():
				users.append(userSkel["name"])

			if users:
				ret = sendEMail(dests=users, template=os.linesep.join((subject, body)), *args, **kwargs)
				success = True
				return ret
			else:
				logging.warning("There are no root-users.")

	except Exception:
		raise

	finally:
		if not success:
			logging.critical("Cannot send mail to Admins.")
			logging.critical("Subject of mail: %s", subject)
			logging.critical("Content of mail: %s", body)

	return False


def getCurrentUser():
	"""
		Retrieve current user, if logged in.

		If a user is logged in, this function returns a dict containing user data.

		If no user is logged in, the function returns None.

		:rtype: dict | bool
		:returns: A dict containing information about the logged-in user, None if no user is logged in.
	"""
	user = None
	if "user" in dir(conf["viur.mainApp"]):  # Check for our custom user-api
		user = conf["viur.mainApp"].user.getCurrentUser()
	return user


def markFileForDeletion(dlkey):
	"""
	Adds a marker to the data store that the file specified as *dlkey* can be deleted.

	Once the mark has been set, the data store is checked four times (default: every 4 hours)
	if the file is in use somewhere. If it is still in use, the mark goes away, otherwise
	the mark and the file are removed from the datastore. These delayed checks are necessary
	due to database inconsistency.

	:type dlkey: str
	:param dlkey: Unique download-key of the file that shall be marked for deletion.
	"""
	fileObj = db.Query("viur-deleted-files").filter("dlkey", dlkey).getEntry()

	if fileObj:  # Its allready marked
		return

	fileObj = db.Entity(db.Key("viur-deleted-files"))
	fileObj["itercount"] = 0
	fileObj["dlkey"] = str(dlkey)
	db.Put(fileObj)


def escapeString(val, maxLength=254):
	"""
		Quotes several characters and removes "\\\\n" and "\\\\0" to prevent XSS injection.

		:param val: The value to be escaped.
		:type val: str

		:param maxLength: Cut-off after maxLength characters. A value of 0 means "unlimited".
		:type maxLength: int

		:returns: The quoted string.
		:rtype: str
	"""
	val = str(val).strip() \
		.replace("<", "&lt;") \
		.replace(">", "&gt;") \
		.replace("\"", "&quot;") \
		.replace("'", "&#39;") \
		.replace("\n", "") \
		.replace("\0", "")

	if maxLength:
		return val[0:maxLength]

	return val


def hmacSign(data: Any) -> str:
	assert conf["viur.file.hmacKey"] is not None, "No hmac-key set!"
	if not isinstance(data, bytes):
		data = str(data).encode("UTF-8")
	return hmac.new(conf["viur.file.hmacKey"], msg=data, digestmod=hashlib.sha3_384).hexdigest()


def hmacVerify(data: Any, signature: str) -> bool:
	return hmac.compare_digest(hmacSign(data), signature)


def downloadUrlFor(folder: str, fileName: str, derived: bool = False,
				   expires: Union[timedelta, None] = timedelta(hours=1)) -> str:
	if derived:
		filePath = "%s/derived/%s" % (folder, fileName)
	else:
		filePath = "%s/source/%s" % (folder, fileName)
	sigStr = "%s\0%s" % (filePath, ((datetime.now() + expires).strftime("%Y%m%d%H%M") if expires else 0))
	sigStr = urlsafe_b64encode(sigStr.encode("UTF-8"))
	resstr = hmacSign(sigStr)
	return "/file/download/%s?sig=%s" % (sigStr.decode("ASCII"), resstr)


def seoUrlToEntry(module, entry=None, skelType=None, language=None):
	from viur.core import conf
	pathComponents = [""]
	lang = currentLanguage.get()
	if conf["viur.languageMethod"] == "url":
		pathComponents.append(lang)
	if module in conf["viur.languageModuleMap"] and lang in conf["viur.languageModuleMap"][module]:
		module = conf["viur.languageModuleMap"][module][lang]
	pathComponents.append(module)
	if not entry:
		return "/".join(pathComponents)
	else:
		try:
			currentSeoKeys = entry["viurCurrentSeoKeys"]
		except:
			return "/".join(pathComponents)
		if lang in (currentSeoKeys or {}):
			pathComponents.append(str(currentSeoKeys[lang]))
		elif "key" in entry:
			pathComponents.append(str(entry["key"]))
		elif "name" in dir(entry):
			pathComponents.append(str(entry.name))
		return "/".join(pathComponents)


def seoUrlToFunction(module, function, render=None):
	from viur.core import conf
	lang = currentLanguage.get()
	if module in conf["viur.languageModuleMap"] and lang in conf["viur.languageModuleMap"][module]:
		module = conf["viur.languageModuleMap"][module][lang]
	if conf["viur.languageMethod"] == "url":
		pathComponents = ["", lang]
	else:
		pathComponents = [""]
	targetObject = conf["viur.mainResolver"]
	if module in targetObject:
		pathComponents.append(module)
		targetObject = targetObject[module]
	if render and render in targetObject:
		pathComponents.append(render)
		targetObject = targetObject[render]
	if function in targetObject:
		func = targetObject[function]
		if getattr(func, "seoLanguageMap", None) and lang in func.seoLanguageMap:
			pathComponents.append(func.seoLanguageMap[lang])
		else:
			pathComponents.append(function)
	return "/".join(pathComponents)


def normalizeKey(key: Union[None, 'db.KeyClass']) -> Union[None, 'db.KeyClass']:
	"""
		Normalizes a datastore key (replacing _application with the current one)

		:param key: Key to be normalized.

		:return: Normalized key in string representation.
	"""
	if key is None:
		return None
	if key.parent:
		parent = normalizeKey(key.parent)
	else:
		parent = None
	return db.Key(key.kind, key.id_or_name, parent=parent)
