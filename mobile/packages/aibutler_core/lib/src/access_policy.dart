/// 访问层级与能力门控（端侧副本）。
///
/// 计划书依据
/// ----------
/// - 七、验收标准 11「未登录状态下所有业务接口拒绝执行」
/// - 三、P7 触发时机「唤醒后先校验本地登录会话状态，未登录引导登录」
///
/// 裁定 U64（人工授权，2026-10-24）
/// ------------------------------
/// 「未登录时两端都只能文字查询，登录后可以语音唤起」。
///
/// **这份表不是权限真值源，只是渲染依据。**
/// 真值源在服务端 `ai_butler/core/access.py`：客户端能被改包、能被 Hook，
/// 拿客户端的能力表当权限判据等于没有权限。因此这里的用途只有一个 ——
/// 决定按钮是「可点」还是「点了提示去登录」，避免用户点了才发现不行。
///
/// 两边不一致时**以服务端为准**：服务端 403 会带 `capability` 与 `message`，
/// 端侧收到就按服务端的说法提示，不拿本地表辩解。
library;

/// 身份层级。只有两级：再细分是服务端 `permissions.UserRole` 的事。
enum AccessTier {
  guest,
  member;

  static AccessTier parse(String? raw) =>
      raw == 'member' ? AccessTier.member : AccessTier.guest;
}

/// 用户能做的一件事。命名与 `ai_butler/core/access.py` 的 `Capability` 一一对应。
enum Capability {
  textQuery('text_query'),
  voiceInput('voice_input'),
  voiceOutput('voice_output'),
  wakeWord('wake_word'),
  speakerprint('speakerprint'),
  visionInput('vision_input'),
  privateRetrieval('private_retrieval'),
  memoryWrite('memory_write'),
  deviceReport('device_report'),
  sensitiveAction('sensitive_action');

  const Capability(this.wire);

  /// 与服务端约定的字符串值。**用 wire 而不是 enum.name**：
  /// Dart 的 camelCase 与 Python 的 snake_case 不同名，靠映射而不是「改一边去迁就」。
  final String wire;

  static Capability? tryParse(String value) {
    for (final capability in Capability.values) {
      if (capability.wire == value) return capability;
    }
    return null;
  }
}

/// 层级被拒时的提示语。**与后端 `UPGRADE_HINT` 同口径**，不另写一套说法。
const Map<Capability, String> kUpgradeHint = {
  Capability.voiceInput: '语音输入需要登录后使用（需绑定账号才能做声纹校验）',
  Capability.voiceOutput: '语音播报需要登录后使用',
  Capability.wakeWord: '语音唤起需要登录后使用',
  Capability.speakerprint: '声纹登记与校验需要登录后使用',
  Capability.visionInput: '图文识别需要登录后使用',
  Capability.privateRetrieval: '检索你自己的私有资料需要登录',
  Capability.memoryWrite: '写入长期记忆需要登录',
  Capability.deviceReport: '设备上报需要登录',
  Capability.sensitiveAction: '该操作需要登录并通过声纹校验',
};

/// 某层级的能力集合。**与后端矩阵保持一致**，由 `test/access_policy_test.dart` 钉死。
const Map<AccessTier, Set<Capability>> kTierCapabilities = {
  AccessTier.guest: {Capability.textQuery},
  AccessTier.member: {
    Capability.textQuery,
    Capability.voiceInput,
    Capability.voiceOutput,
    Capability.wakeWord,
    Capability.speakerprint,
    Capability.visionInput,
    Capability.privateRetrieval,
    Capability.memoryWrite,
    Capability.deviceReport,
    Capability.sensitiveAction,
  },
};

/// 该层级的能力集合（只读副本）。
Set<Capability> capabilitiesOf(AccessTier tier) =>
    Set<Capability>.unmodifiable(kTierCapabilities[tier] ?? const <Capability>{});

/// 能力不足。**带上能力与提示**，UI 才能说清「为什么点不了」。
class AccessDeniedException implements Exception {
  AccessDeniedException(this.capability, {this.tier = AccessTier.guest, String? message})
      : message = message ?? kUpgradeHint[capability] ?? '该功能需要登录后使用';

  final Capability capability;
  final AccessTier tier;
  final String message;

  /// 需要的是「去登录」，而不是「功能坏了」。
  bool get needsLogin => tier == AccessTier.guest;

  @override
  String toString() => 'AccessDeniedException(${capability.wire}): $message';
}

/// 当前身份的能力档案。服务端 `/api/access/profile` 的解析结果。
class AccessProfile {
  const AccessProfile({
    required this.tier,
    required this.capabilities,
    this.textQueryRemaining,
  });

  /// 本地兜底：未拿到服务端档案时按**最严**处理（访客），绝不放宽。
  factory AccessProfile.fallback() => AccessProfile(
        tier: AccessTier.guest,
        capabilities: capabilitiesOf(AccessTier.guest),
      );

  factory AccessProfile.guest({int? textQueryRemaining}) => AccessProfile(
        tier: AccessTier.guest,
        capabilities: capabilitiesOf(AccessTier.guest),
        textQueryRemaining: textQueryRemaining,
      );

  factory AccessProfile.member() => AccessProfile(
        tier: AccessTier.member,
        capabilities: capabilitiesOf(AccessTier.member),
      );

  /// 解析服务端下发的档案。
  ///
  /// 服务端给了 `capabilities` 就用服务端那份 —— 它是权威；
  /// 只给了 `tier` 时退回本地矩阵。**解析失败退回访客**，不退回成员。
  factory AccessProfile.fromJson(Map<String, dynamic> json) {
    final tier = AccessTier.parse(json['tier'] as String?);
    final raw = json['capabilities'];
    final parsed = <Capability>{};
    if (raw is List) {
      for (final item in raw) {
        final capability = Capability.tryParse(item.toString());
        if (capability != null) parsed.add(capability);
      }
    }
    return AccessProfile(
      tier: tier,
      capabilities: parsed.isEmpty ? capabilitiesOf(tier) : parsed,
      textQueryRemaining: (json['text_query_remaining'] as num?)?.toInt(),
    );
  }

  final AccessTier tier;
  final Set<Capability> capabilities;

  /// 访客剩余问答次数；成员为 null（不限额）。
  final int? textQueryRemaining;

  bool allows(Capability capability) => capabilities.contains(capability);

  bool get isGuest => tier == AccessTier.guest;
  bool get isMember => tier == AccessTier.member;

  /// 是否开启了语音唤起（登录后才有）。
  bool get canVoice => allows(Capability.voiceInput);
  bool get canWake => allows(Capability.wakeWord);
  bool get canImage => allows(Capability.visionInput);
  bool get canSpeakerprint => allows(Capability.speakerprint);

  /// 访客额度是否已用完（用完时提示登录，而不是让用户反复失败）。
  bool get quotaExhausted => textQueryRemaining != null && textQueryRemaining! <= 0;

  /// 拿能力，不合适就抛。**UI 与业务都走这一条**，不各自写 if。
  void require(Capability capability) {
    if (!allows(capability)) {
      throw AccessDeniedException(capability, tier: tier);
    }
  }

  AccessProfile copyWith({int? textQueryRemaining}) => AccessProfile(
        tier: tier,
        capabilities: capabilities,
        textQueryRemaining: textQueryRemaining ?? this.textQueryRemaining,
      );

  @override
  String toString() =>
      'AccessProfile(${tier.name}, ${capabilities.map((c) => c.wire).toList()})';
}
