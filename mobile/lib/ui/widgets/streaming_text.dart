/// 逐字文字渲染（三、P7 步骤5「逐字文字渲染 + 同步 TTS 播报」）。
library;

import 'package:flutter/material.dart';

class StreamingText extends StatelessWidget {
  const StreamingText({
    super.key,
    required this.text,
    this.riskNoted = false,
    this.streaming = false,
  });

  final String text;

  /// 该条带风险标注时用醒目样式（来自 P5/P6 的风险标注透传）。
  final bool riskNoted;
  final bool streaming;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: Text(
            text,
            style: theme.textTheme.bodyLarge?.copyWith(
              color: riskNoted ? theme.colorScheme.error : null,
            ),
          ),
        ),
        if (streaming)
          const Padding(
            padding: EdgeInsets.only(left: 6),
            child: SizedBox(
              width: 10, height: 10,
              child: CircularProgressIndicator(strokeWidth: 2),
            ),
          ),
        if (riskNoted)
          Padding(
            padding: const EdgeInsets.only(left: 6),
            child: Icon(Icons.warning_amber_rounded,
                size: 18, color: theme.colorScheme.error),
          ),
      ],
    );
  }
}
