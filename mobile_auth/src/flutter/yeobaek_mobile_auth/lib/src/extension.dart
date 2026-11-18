import 'dart:io';
import 'dart:math' as math;

import 'package:flet/flet.dart';
import 'package:flutter/foundation.dart';
import 'package:google_sign_in/google_sign_in.dart';
import 'package:path_provider/path_provider.dart';
import 'package:pdfx/pdfx.dart';

class Extension extends FletExtension {
  @override
  FletService? createService(Control control) {
    if (control.type == 'YeobaekGoogleDriveAuth') {
      return _DriveAuthService(control: control);
    }
    if (control.type == 'YeobaekMobilePdfPreview') {
      return _MobilePdfPreviewService(control: control);
    }
    return null;
  }
}

class _MobilePdfPreviewService extends FletService {
  _MobilePdfPreviewService({required super.control});

  Future<void> _queue = Future.value();

  @override
  void init() {
    super.init();
    control.addInvokeMethodListener(_invokeMethod);
  }

  @override
  void dispose() {
    control.removeInvokeMethodListener(_invokeMethod);
    super.dispose();
  }

  Future<dynamic> _invokeMethod(String name, dynamic args) {
    final pending = _queue.then((_) => _renderPage(name, args));
    _queue = pending.then<void>((_) {}, onError: (Object _, StackTrace _) {});
    return pending;
  }

  Future<List<Object>> _renderPage(String name, dynamic args) async {
    if (name != 'page' || args is! Map) {
      throw UnsupportedError('Unknown PDF preview method.');
    }
    if (kIsWeb ||
        (defaultTargetPlatform != TargetPlatform.android &&
            defaultTargetPlatform != TargetPlatform.iOS)) {
      throw UnsupportedError('Mobile PDF preview requires Android or iOS.');
    }
    final data = args['data'];
    final index = args['index'];
    final width = args['width'];
    if (data is! Uint8List ||
        data.isEmpty ||
        data.length > 64 * 1024 * 1024 ||
        index is! int ||
        index < 0 ||
        width is! int ||
        width < 1 ||
        width > 1600) {
      throw ArgumentError('Invalid PDF preview input.');
    }

    if (defaultTargetPlatform == TargetPlatform.android) {
      // PdfRenderer needs a file descriptor. Own this file so closing the
      // page also removes its source PDF from the app cache.
      final cache = await getTemporaryDirectory();
      final directory = await cache.createTemp('yeobaek-pdf-');
      try {
        final file = File('${directory.path}/document.pdf');
        await file.writeAsBytes(data);
        return await _renderDocument(
          await PdfDocument.openFile(file.path),
          index,
          width,
        );
      } finally {
        await directory.delete(recursive: true);
      }
    }
    return _renderDocument(await PdfDocument.openData(data), index, width);
  }

  Future<List<Object>> _renderDocument(
    PdfDocument document,
    int index,
    int width,
  ) async {
    try {
      if (index >= document.pagesCount) {
        throw RangeError.range(index, 0, document.pagesCount - 1, 'index');
      }
      final page = await document.getPage(index + 1);
      try {
        if (!page.width.isFinite ||
            !page.height.isFinite ||
            page.width <= 0 ||
            page.height <= 0) {
          throw StateError('Invalid PDF page dimensions.');
        }
        final scale = math.min(width / page.width, 1800 / page.height);
        final image = await page.render(
          width: math.max(1.0, page.width * scale),
          height: math.max(1.0, page.height * scale),
          format: PdfPageImageFormat.png,
          backgroundColor: '#FFFFFF',
        );
        if (image == null || image.bytes.isEmpty) {
          throw StateError('Could not render the PDF page.');
        }
        return [image.bytes, document.pagesCount];
      } finally {
        await page.close();
      }
    } finally {
      await document.close();
    }
  }
}

class _DriveAuthService extends FletService {
  _DriveAuthService({required super.control});

  static const _scopes = ['https://www.googleapis.com/auth/drive.appdata'];
  static final _signIn = GoogleSignIn.instance;
  static Future<void>? _initialization;
  static (String?, String?)? _clientIds;

  Future<void> _queue = Future.value();
  GoogleSignInAccount? _account;
  String? _lastToken;
  bool _triedRestore = false;
  bool _signedOut = false;

  @override
  void init() {
    super.init();
    control.addInvokeMethodListener(_invokeMethod);
  }

  @override
  void dispose() {
    control.removeInvokeMethodListener(_invokeMethod);
    super.dispose();
  }

  Future<dynamic> _invokeMethod(String name, dynamic args) {
    final pending = _queue.then((_) => _handleMethod(name, args));
    _queue = pending.then<void>((_) {}, onError: (Object _, StackTrace _) {});
    return pending;
  }

  Future<dynamic> _handleMethod(String name, dynamic args) async {
    try {
      switch (name) {
        case 'token':
          return await _token(interactive: args['interactive'] == true);
        case 'clear_token':
          final token = _lastToken;
          _lastToken = null;
          if (token != null) {
            await _authorization.clearAuthorizationToken(accessToken: token);
          }
          return null;
        case 'sign_out':
          _signedOut = true;
          _account = null;
          _lastToken = null;
          await _initialize();
          await _signIn.signOut();
          return null;
        default:
          throw UnsupportedError('Unknown Google Drive authorization method.');
      }
    } on GoogleSignInException catch (error) {
      if (name == 'token' && error.code == GoogleSignInExceptionCode.canceled) {
        return null;
      }
      throw StateError('Google Sign-In failed (${error.code.name}).');
    }
  }

  Future<void> _initialize() async {
    if (kIsWeb ||
        (defaultTargetPlatform != TargetPlatform.android &&
            defaultTargetPlatform != TargetPlatform.iOS)) {
      throw UnsupportedError(
        'Google Drive mobile authorization requires Android or iOS.',
      );
    }
    final ids = (
      control.getString('client_id'),
      control.getString('server_client_id'),
    );
    if (_clientIds != null && _clientIds != ids) {
      throw StateError(
        'Restart Yeobaek after changing Google OAuth client IDs.',
      );
    }
    _clientIds = ids;
    await (_initialization ??= _signIn.initialize(
      clientId: ids.$1,
      serverClientId: ids.$2,
    ));
  }

  GoogleSignInAuthorizationClient get _authorization =>
      _account?.authorizationClient ?? _signIn.authorizationClient;

  Future<String?> _token({required bool interactive}) async {
    if (_signedOut && !interactive) return null;
    await _initialize();

    if (interactive && _account == null) {
      _account = await _signIn.authenticate();
      _signedOut = false;
    } else if (!_triedRestore &&
        _account == null &&
        defaultTargetPlatform == TargetPlatform.iOS) {
      _account = await _signIn.attemptLightweightAuthentication();
      _triedRestore = true;
    }

    final client = _authorization;
    var authorization = await client.authorizationForScopes(_scopes);
    if (authorization == null && interactive) {
      authorization = await client.authorizeScopes(_scopes);
    }
    _lastToken = authorization?.accessToken;
    return _lastToken;
  }
}
