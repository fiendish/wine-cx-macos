// SPDX-License-Identifier: MIT

#import <Cocoa/Cocoa.h>

static NSString *shellQuote(NSString *value)
{
    return [NSString stringWithFormat:@"'%@'", [value stringByReplacingOccurrencesOfString:@"'" withString:@"'\\''"]];
}

static NSString *appleScriptQuote(NSString *value)
{
    value = [value stringByReplacingOccurrencesOfString:@"\\" withString:@"\\\\"];
    return [value stringByReplacingOccurrencesOfString:@"\"" withString:@"\\\""];
}

static void showError(NSString *message)
{
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = @"Wine CX could not start";
    alert.informativeText = message;
    alert.alertStyle = NSAlertStyleCritical;
    [alert runModal];
}

static BOOL wineTerminalIsRunning(void)
{
    NSString *marker = [NSHomeDirectory() stringByAppendingPathComponent:
        @"Library/Caches/com.github.fiendish.wine-cx/terminal.pid"];
    NSString *contents = [NSString stringWithContentsOfFile:marker
                                                   encoding:NSUTF8StringEncoding
                                                      error:nil];
    NSArray<NSString *> *lines = [contents componentsSeparatedByCharactersInSet:
        NSCharacterSet.newlineCharacterSet];
    if (lines.count < 2 || lines[0].length == 0 || lines[1].length == 0 ||
        [lines[0] rangeOfCharacterFromSet:NSCharacterSet.decimalDigitCharacterSet.invertedSet].location != NSNotFound ||
        [NSRunningApplication runningApplicationsWithBundleIdentifier:@"com.apple.Terminal"].count == 0) {
        return NO;
    }

    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:@"/bin/ps"];
    task.arguments = @[@"-p", lines[0], @"-o", @"lstart="];
    NSPipe *output = [NSPipe pipe];
    task.standardOutput = output;
    NSError *error = nil;
    if (![task launchAndReturnError:&error]) {
        showError(error.localizedDescription);
        return NO;
    }
    NSData *data = [output.fileHandleForReading readDataToEndOfFile];
    [task waitUntilExit];
    if (task.terminationStatus != 0) {
        return NO;
    }
    NSString *start = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    return [[start stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet]
            isEqualToString:[lines[1] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet]];
}

@interface AppDelegate : NSObject <NSApplicationDelegate>
@property BOOL openedFile;
@end

@implementation AppDelegate

- (BOOL)runTerminalScript:(NSString *)source
{
    NSDictionary *error = nil;
    NSAppleScript *script = [[NSAppleScript alloc] initWithSource:source];
    if (![script executeAndReturnError:&error]) {
        showError(error.description);
        return NO;
    }
    return YES;
}

- (BOOL)openWineTerminal
{
    if (wineTerminalIsRunning()) {
        return [self runTerminalScript:@"tell application \"Terminal\" to activate"];
    }
    NSString *resources = NSBundle.mainBundle.resourcePath;
    NSString *wineBin = [resources stringByAppendingPathComponent:@"wine/bin"];
    NSString *helper = [resources stringByAppendingPathComponent:@"wine-terminal-help"];
    NSString *command = [NSString stringWithFormat:@"export PATH=%@:\"$PATH\"; %@",
                         shellQuote(wineBin), shellQuote(helper)];
    NSString *source = [NSString stringWithFormat:
        @"tell application \"Terminal\"\n"
         @"set wineTab to do script \"%@\"\n"
         @"set custom title of wineTab to \"Wine CX\"\n"
         @"activate\n"
         @"end tell",
        appleScriptQuote(command)];
    if (![self runTerminalScript:source]) {
        return NO;
    }
    return YES;
}

- (void)application:(NSApplication *)application openFiles:(NSArray<NSString *> *)filenames
{
    self.openedFile = YES;
    NSString *wine = [[NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"wine/bin/wine"] stringByStandardizingPath];
    for (NSString *filename in filenames) {
        NSTask *task = [[NSTask alloc] init];
        task.executableURL = [NSURL fileURLWithPath:wine];
        task.arguments = @[filename];
        task.currentDirectoryURL = [NSURL fileURLWithPath:[filename stringByDeletingLastPathComponent]
                                               isDirectory:YES];
        NSError *error = nil;
        if (![task launchAndReturnError:&error]) {
            showError(error.localizedDescription);
            [application replyToOpenOrPrint:NSApplicationDelegateReplyFailure];
            [application terminate:nil];
            return;
        }
    }
    [application replyToOpenOrPrint:NSApplicationDelegateReplySuccess];
    if (application.isRunning) {
        [application terminate:nil];
    }
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification
{
    if (self.openedFile) {
        [NSApp terminate:nil];
        return;
    }
    [self openWineTerminal];
    [NSApp terminate:nil];
}

@end

int main(int argc, const char *argv[])
{
    @autoreleasepool {
        NSApplication *application = NSApplication.sharedApplication;
        AppDelegate *delegate = [[AppDelegate alloc] init];
        application.delegate = delegate;
        return NSApplicationMain(argc, argv);
    }
}
